[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateNotNullOrEmpty()]
    [string]$RepositoryPath = (Get-Location).Path,

    [ValidateNotNullOrEmpty()]
    [string]$RemoteName = 'origin',

    [ValidateNotNullOrEmpty()]
    [string]$StageBranch = 'chatgpt/stage3-integration',

    [switch]$SkipFetch,

    [switch]$InstallPSScriptAnalyzer,

    [switch]$KeepWorktree
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

function Write-Step {
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$Message
    )

    Write-Output ''
    Write-Output ('=== {0} ===' -f $Message)
}

function Get-ApplicationPath {
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$Name
    )

    $command = Get-Command -Name $Name -CommandType Application -ErrorAction Stop
    return $command.Path
}

function Invoke-NativeChecked {
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$Executable,

        [Parameter(Mandatory)]
        [AllowEmptyCollection()]
        [string[]]$Arguments,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$WorkingDirectory,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$Description,

        [int[]]$AllowedExitCodes = @(0)
    )

    Write-Output ('Running: {0}' -f $Description)

    Push-Location -LiteralPath $WorkingDirectory
    try {
        & $Executable @Arguments
        $nativeExitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }

    if ($nativeExitCode -notin $AllowedExitCodes) {
        throw (
            '{0} failed with exit code {1}.' -f
            $Description,
            $nativeExitCode
        )
    }
}

function Invoke-NativeCaptured {
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$Executable,

        [Parameter(Mandatory)]
        [AllowEmptyCollection()]
        [string[]]$Arguments,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$WorkingDirectory,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$Description,

        [int[]]$AllowedExitCodes = @(0)
    )

    Push-Location -LiteralPath $WorkingDirectory
    try {
        $output = & $Executable @Arguments 2>&1
        $nativeExitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }

    if ($nativeExitCode -notin $AllowedExitCodes) {
        if ($output) {
            $output | Write-Output
        }

        throw (
            '{0} failed with exit code {1}.' -f
            $Description,
            $nativeExitCode
        )
    }

    return [pscustomobject]@{
        ExitCode = $nativeExitCode
        Output = @($output)
    }
}

function Test-RemovedPath {
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$Root,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$RelativePath
    )

    $path = Join-Path -Path $Root -ChildPath $RelativePath

    if (Test-Path -LiteralPath $path) {
        throw ('Removed Stage 3 path is still present: {0}' -f $RelativePath)
    }
}

function Test-RequiredPath {
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$Root,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$RelativePath
    )

    $path = Join-Path -Path $Root -ChildPath $RelativePath

    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw ('Required path is missing: {0}' -f $RelativePath)
    }
}

if ([string]::IsNullOrWhiteSpace($RepositoryPath)) {
    throw 'RepositoryPath is empty.'
}

if ([string]::IsNullOrWhiteSpace($RemoteName)) {
    throw 'RemoteName is empty.'
}

if ([string]::IsNullOrWhiteSpace($StageBranch)) {
    throw 'StageBranch is empty.'
}

if (-not (Test-Path -LiteralPath $RepositoryPath -PathType Container)) {
    throw ('Repository directory does not exist: {0}' -f $RepositoryPath)
}

$repositoryItem = Get-Item -LiteralPath $RepositoryPath -Force -ErrorAction Stop

if ($repositoryItem.PSProvider.Name -ne 'FileSystem') {
    throw ('RepositoryPath is not a file-system path: {0}' -f $RepositoryPath)
}

$RepositoryPath = Convert-Path -LiteralPath $RepositoryPath
$gitExecutable = Get-ApplicationPath -Name 'git'
$uvExecutable = Get-ApplicationPath -Name 'uv'

$gitArguments = @(
    '-C'
    $RepositoryPath
    'rev-parse'
    '--is-inside-work-tree'
)

$workTreeCheckParameters = @{
    Executable = $gitExecutable
    Arguments = $gitArguments
    WorkingDirectory = $RepositoryPath
    Description = 'Validate Git worktree'
}

$workTreeCheck = Invoke-NativeCaptured @workTreeCheckParameters
$isWorkTree = [string]($workTreeCheck.Output | Select-Object -Last 1)

if ($isWorkTree.Trim() -ne 'true') {
    throw ('Directory is not a Git worktree: {0}' -f $RepositoryPath)
}

$branchValidationArguments = @(
    'check-ref-format'
    '--branch'
    $StageBranch
)

$branchValidationParameters = @{
    Executable = $gitExecutable
    Arguments = $branchValidationArguments
    WorkingDirectory = $RepositoryPath
    Description = 'Validate Stage branch name'
}

$branchValidation = Invoke-NativeCaptured @branchValidationParameters
$validatedBranchName = [string]($branchValidation.Output | Select-Object -Last 1)

if ($validatedBranchName -cne $StageBranch) {
    throw ('Invalid Stage branch name: {0}' -f $StageBranch)
}

$remoteValidationArguments = @(
    '-C'
    $RepositoryPath
    'remote'
    'get-url'
    $RemoteName
)

$remoteValidationParameters = @{
    Executable = $gitExecutable
    Arguments = $remoteValidationArguments
    WorkingDirectory = $RepositoryPath
    Description = 'Validate Git remote'
}

$remoteValidation = Invoke-NativeCaptured @remoteValidationParameters
$remoteUrl = [string]($remoteValidation.Output | Select-Object -Last 1)

if ([string]::IsNullOrWhiteSpace($remoteUrl)) {
    throw ('Git remote has no URL: {0}' -f $RemoteName)
}

Write-Output ('Repository: {0}' -f $RepositoryPath)
Write-Output ('Remote: {0}' -f $RemoteName)
Write-Output ('Remote URL: {0}' -f $remoteUrl)
Write-Output ('Stage branch: {0}' -f $StageBranch)

if (-not $SkipFetch) {
    Write-Step -Message 'Fetch the Stage 3 branch'

    $fetchRefSpec = 'refs/heads/{0}:refs/remotes/{1}/{0}' -f $StageBranch, $RemoteName
    $fetchArguments = @(
        '-C'
        $RepositoryPath
        'fetch'
        '--no-tags'
        $RemoteName
        $fetchRefSpec
    )

    $fetchParameters = @{
        Executable = $gitExecutable
        Arguments = $fetchArguments
        WorkingDirectory = $RepositoryPath
        Description = 'Fetch Stage 3 branch'
    }

    Invoke-NativeChecked @fetchParameters
}

$remoteRef = 'refs/remotes/{0}/{1}' -f $RemoteName, $StageBranch
$verifyRefArguments = @(
    '-C'
    $RepositoryPath
    'rev-parse'
    '--verify'
    ('{0}^{{commit}}' -f $remoteRef)
)

$verifyRefParameters = @{
    Executable = $gitExecutable
    Arguments = $verifyRefArguments
    WorkingDirectory = $RepositoryPath
    Description = 'Resolve Stage 3 commit'
}

$verifiedRef = Invoke-NativeCaptured @verifyRefParameters
$stageCommit = [string]($verifiedRef.Output | Select-Object -Last 1)

if ([string]::IsNullOrWhiteSpace($stageCommit)) {
    throw ('Unable to resolve Stage 3 ref: {0}' -f $remoteRef)
}

$tempRoot = [System.IO.Path]::GetTempPath()
$worktreeName = 'AzurPilot-Stage3-Verify-{0}' -f ([Guid]::NewGuid().ToString('N'))
$worktreePath = Join-Path -Path $tempRoot -ChildPath $worktreeName
$worktreeCreated = $false
$verificationSucceeded = $false

try {
    Write-Step -Message 'Create an isolated verification worktree'

    $worktreeArguments = @(
        '-C'
        $RepositoryPath
        'worktree'
        'add'
        '--detach'
        $worktreePath
        $stageCommit
    )

    $worktreeParameters = @{
        Executable = $gitExecutable
        Arguments = $worktreeArguments
        WorkingDirectory = $RepositoryPath
        Description = 'Create isolated worktree'
    }

    Invoke-NativeChecked @worktreeParameters
    $worktreeCreated = $true

    Write-Output ('Worktree: {0}' -f $worktreePath)
    Write-Output ('Commit: {0}' -f $stageCommit)

    Write-Step -Message 'Verify removed and preserved paths'

    $removedPaths = @(
        '.github/scripts/build_git_over_cdn.py'
        '.github/scripts/build_git_over_cdn_eo_esa.mjs'
        '.github/scripts/package.json'
        '.github/scripts/upload_123pan.py'
        '.github/workflows/cloudflare-pages-git-over-cdn.sh'
        '.github/workflows/git-over-cdn-123pan.yml'
        '.github/workflows/git-over-cdn-ssh.yml'
        'deploy/Windows/git.py'
        'deploy/Windows/installer_test.py'
        'deploy/geo.py'
        'deploy/git.py'
        'deploy/git_over_cdn/client.py'
        'deploy/git_over_cdn/endpoints.py'
        'deploy/installer.py'
        'module/statistics/cl1_data_submitter.py'
        'module/webui/app_developer_update.py'
        'module/webui/updater.py'
        'tests/test_git_over_cdn.py'
        'tests/test_webui_updater.py'
    )

    foreach ($removedPath in $removedPaths) {
        Test-RemovedPath -Root $worktreePath -RelativePath $removedPath
    }

    $requiredPaths = @(
        'module/base/api_client.py'
        'module/daemon/uncensored.py'
        'module/webui/app_lifecycle.py'
        'module/webui/process_manager.py'
        'scripts/Start-AzurPilot.ps1'
        'scripts/Verify-AzurPilot-Stage3.ps1'
        'tests/test_deploy_location.py'
        'tests/test_stage3d_deploy_set.py'
        'tests/test_stage3d_legacy_installer.py'
        'tests/test_stage3d_templates.py'
        'tests/test_stage3d_webui_settings.py'
        'tests/test_stage3e_release_cleanup.py'
    )

    foreach ($requiredPath in $requiredPaths) {
        Test-RequiredPath -Root $worktreePath -RelativePath $requiredPath
    }

    Write-Step -Message 'Check active-code residue'

    $residuePattern = 'module\.webui\.updater|update_alas|git_over_cdn|cl1_data_submitter|clarity\.ms|Microsoft Clarity'
    $grepArguments = @(
        '-C'
        $worktreePath
        'grep'
        '-n'
        '-E'
        $residuePattern
        '--'
        'alas.py'
        'mcp_server_sse.py'
        'deploy'
        'module'
        'assets/gui'
    )

    $grepParameters = @{
        Executable = $gitExecutable
        Arguments = $grepArguments
        WorkingDirectory = $worktreePath
        Description = 'Search active-code residue'
        AllowedExitCodes = @(0, 1)
    }

    $residueResult = Invoke-NativeCaptured @grepParameters

    if ($residueResult.ExitCode -eq 0) {
        $residueResult.Output | Write-Output
        throw 'Removed updater, telemetry, or Git-over-CDN residue remains in active code.'
    }

    Write-Step -Message 'Synchronize Python dependencies'

    $syncArguments = @(
        'sync'
    )

    $syncParameters = @{
        Executable = $uvExecutable
        Arguments = $syncArguments
        WorkingDirectory = $worktreePath
        Description = 'uv sync'
    }

    Invoke-NativeChecked @syncParameters

    Write-Step -Message 'Run Python static checks'

    $ruffArguments = @(
        'run'
        'ruff'
        'check'
        '.'
        '--select'
        'E9,F63,F7,F82'
        '--ignore'
        'F821,F722'
    )

    $ruffParameters = @{
        Executable = $uvExecutable
        Arguments = $ruffArguments
        WorkingDirectory = $worktreePath
        Description = 'Ruff Stage 3 syntax and static checks'
    }

    Invoke-NativeChecked @ruffParameters

    Write-Step -Message 'Run Stage 3 regression tests'

    $testArguments = @(
        'run'
        'python'
        '-m'
        'unittest'
        '-v'
        'tests/test_deploy_location.py'
        'tests/test_stage3d_deploy_set.py'
        'tests/test_stage3d_legacy_installer.py'
        'tests/test_stage3d_templates.py'
        'tests/test_stage3d_webui_settings.py'
        'tests/test_stage3e_release_cleanup.py'
    )

    $testParameters = @{
        Executable = $uvExecutable
        Arguments = $testArguments
        WorkingDirectory = $worktreePath
        Description = 'Stage 3 unittest suite'
    }

    Invoke-NativeChecked @testParameters

    Write-Step -Message 'Verify generated configuration files'

    $buttonArguments = @(
        'run'
        '-m'
        'dev_tools.button_extract'
    )

    $buttonParameters = @{
        Executable = $uvExecutable
        Arguments = $buttonArguments
        WorkingDirectory = $worktreePath
        Description = 'Generate button configuration'
    }

    Invoke-NativeChecked @buttonParameters

    $configArguments = @(
        'run'
        '-m'
        'module.config.config_updater'
    )

    $configParameters = @{
        Executable = $uvExecutable
        Arguments = $configArguments
        WorkingDirectory = $worktreePath
        Description = 'Generate configuration files'
    }

    Invoke-NativeChecked @configParameters

    $generatedDiffArguments = @(
        '-C'
        $worktreePath
        'diff'
        '--exit-code'
        '--ignore-space-at-eol'
    )

    $generatedDiffParameters = @{
        Executable = $gitExecutable
        Arguments = $generatedDiffArguments
        WorkingDirectory = $worktreePath
        Description = 'Check generated-file diff'
    }

    Invoke-NativeChecked @generatedDiffParameters

    Write-Step -Message 'Parse PowerShell scripts'

    $scriptRoot = Join-Path -Path $worktreePath -ChildPath 'scripts'
    $scriptFiles = @(
        Get-ChildItem -LiteralPath $scriptRoot -Recurse -File |
            Where-Object {
                $_.Extension -in @(
                    '.ps1'
                    '.psm1'
                )
            }
    )

    $parseFailures = [System.Collections.Generic.List[string]]::new()

    foreach ($scriptFile in $scriptFiles) {
        $tokens = $null
        $parseErrors = $null

        [void][System.Management.Automation.Language.Parser]::ParseFile(
            $scriptFile.FullName,
            [ref]$tokens,
            [ref]$parseErrors
        )

        foreach ($parseError in $parseErrors) {
            $parseFailures.Add(
                '{0}:{1}: {2}' -f
                $scriptFile.FullName,
                $parseError.Extent.StartLineNumber,
                $parseError.Message
            )
        }
    }

    if ($parseFailures.Count -gt 0) {
        throw ($parseFailures -join [Environment]::NewLine)
    }

    Write-Step -Message 'Run PSScriptAnalyzer'

    $analyzerModule = Get-Module -ListAvailable -Name 'PSScriptAnalyzer' |
        Sort-Object -Property Version -Descending |
        Select-Object -First 1

    if ($null -eq $analyzerModule -and $InstallPSScriptAnalyzer) {
        $installModuleParameters = @{
            Name = 'PSScriptAnalyzer'
            Repository = 'PSGallery'
            Scope = 'CurrentUser'
            Force = $true
            AllowClobber = $true
            ErrorAction = 'Stop'
        }

        Install-Module @installModuleParameters

        $analyzerModule = Get-Module -ListAvailable -Name 'PSScriptAnalyzer' |
            Sort-Object -Property Version -Descending |
            Select-Object -First 1
    }

    if ($null -eq $analyzerModule) {
        throw (
            'PSScriptAnalyzer is not installed. Re-run with ' +
            '-InstallPSScriptAnalyzer or install it manually.'
        )
    }

    Import-Module -Name $analyzerModule.Path -Force -ErrorAction Stop

    $analyzerParameters = @{
        Path = $scriptRoot
        Recurse = $true
        Severity = @(
            'Error'
            'Warning'
        )
        ExcludeRule = @(
            'PSUseBOMForUnicodeEncodedFile'
        )
    }

    $analysisFindings = @(
        Invoke-ScriptAnalyzer @analyzerParameters
    )

    if ($analysisFindings.Count -gt 0) {
        $analysisFindings |
            Format-Table -AutoSize |
            Out-String |
            Write-Output

        throw 'PSScriptAnalyzer reported errors or warnings.'
    }

    Write-Step -Message 'Final result'
    Write-Output ('Stage 3 verification passed for commit {0}.' -f $stageCommit)
    Write-Output 'No tracked files in the primary worktree were changed.'
    $verificationSucceeded = $true
}
finally {
    if ($worktreeCreated -and -not $KeepWorktree) {
        try {
            Write-Step -Message 'Remove the isolated verification worktree'

            $removeArguments = @(
                '-C'
                $RepositoryPath
                'worktree'
                'remove'
                '--force'
                $worktreePath
            )

            $removeParameters = @{
                Executable = $gitExecutable
                Arguments = $removeArguments
                WorkingDirectory = $RepositoryPath
                Description = 'Remove isolated worktree'
            }

            Invoke-NativeChecked @removeParameters
        }
        catch {
            $cleanupMessage = (
                'Failed to remove isolated verification worktree {0}: {1}' -f
                $worktreePath,
                $_.Exception.Message
            )

            if ($verificationSucceeded) {
                throw $cleanupMessage
            }

            Write-Warning $cleanupMessage
        }
    }
    elseif ($worktreeCreated) {
        Write-Output ('Verification worktree retained: {0}' -f $worktreePath)
    }
}
