@echo off
setlocal EnableExtensions

set "AZUR_PY="
where py >nul 2>&1
if not errorlevel 1 (
    py -3 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "AZUR_PY=py -3"
)
if not defined AZUR_PY (
    where python >nul 2>&1
    if not errorlevel 1 set "AZUR_PY=python"
)
if not defined AZUR_PY (
    where python3 >nul 2>&1
    if not errorlevel 1 set "AZUR_PY=python3"
)
if not defined AZUR_PY (
    echo AzurPilot hook: Python interpreter недоступен 1>&2
    exit /b 2
)

%AZUR_PY% "%~dp0codex_workflow_guards.py"
