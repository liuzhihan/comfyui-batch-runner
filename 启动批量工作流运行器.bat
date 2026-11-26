@echo off
rem ---------------------------------------------------------------
rem  ComfyUI Batch Workflow Runner - launcher
rem  Keep this file pure ASCII: cmd.exe reads .bat files using the
rem  system ANSI code page, so non-ASCII bytes here get garbled.
rem ---------------------------------------------------------------
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"

rem Look for Python: bundled runtime next to this script, then one level up
rem (ComfyUI bundle layout), then pythonw/python from PATH.
set "PY="
if exist "%~dp0python_dapao312\pythonw.exe" set "PY=%~dp0python_dapao312\pythonw.exe"
if not defined PY if exist "%~dp0python_dapao312\python.exe" set "PY=%~dp0python_dapao312\python.exe"
if not defined PY if exist "%~dp0..\python_dapao312\pythonw.exe" set "PY=%~dp0..\python_dapao312\pythonw.exe"
if not defined PY if exist "%~dp0..\python_dapao312\python.exe" set "PY=%~dp0..\python_dapao312\python.exe"
if not defined PY where pythonw >nul 2>nul && set "PY=pythonw"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
    echo.
    echo   Python not found.
    echo   Install Python 3.9 or newer, tick "Add Python to PATH",
    echo   then run this file again.
    echo.
    pause
    exit /b 1
)

start "" "%PY%" "%~dp0comfy_batch_runner.py"
exit /b 0
