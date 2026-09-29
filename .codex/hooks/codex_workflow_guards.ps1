$hookPath = Join-Path $PSScriptRoot "codex_workflow_guards.py"
$pythonCandidates = @(
    @{ Name = "py"; Prefix = @("-3") },
    @{ Name = "python"; Prefix = @() },
    @{ Name = "python3"; Prefix = @() }
)

foreach ($candidate in $pythonCandidates) {
    $interpreter = Get-Command $candidate.Name -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $interpreter) {
        continue
    }

    $probeArguments = @($candidate.Prefix) + @("-c", "import sys")
    & $interpreter.Source @probeArguments *> $null
    if ($LASTEXITCODE -ne 0) {
        continue
    }

    $hookArguments = @($candidate.Prefix) + @($hookPath)
    & $interpreter.Source @hookArguments
    exit $LASTEXITCODE
}

[Console]::Error.WriteLine("AzurPilot hook: Python interpreter недоступен")
exit 2
