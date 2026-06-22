@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem Modern single-window launcher (customtkinter) for all backend services + web console.
rem Shows live colored logs, status pills, and convenient controls.
set "SCRIPT_DIR=%~dp0"

cd /d "%SCRIPT_DIR%"

if exist "%SCRIPT_DIR%venv\Scripts\activate.bat" (
  call "%SCRIPT_DIR%venv\Scripts\activate.bat"
) else if exist "%SCRIPT_DIR%venv\Scripts\activate" (
  call "%SCRIPT_DIR%venv\Scripts\activate"
) else (
  echo [WARN] Python venv not found at "%SCRIPT_DIR%venv". Continuing with the current Python interpreter.
)

python "%SCRIPT_DIR%tk_launcher.py"
pause
