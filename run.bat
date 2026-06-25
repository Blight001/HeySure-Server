@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem Modern single-window launcher (customtkinter) for all backend services + web console.
rem Shows live colored logs, status pills, and convenient controls.
set "SCRIPT_DIR=%~dp0"

cd /d "%SCRIPT_DIR%"

set "PYTHON_CMD="
where python >nul 2>nul && set "PYTHON_CMD=python"
if not defined PYTHON_CMD (
  where py >nul 2>nul && set "PYTHON_CMD=py -3"
)
if not defined PYTHON_CMD (
  echo [ERROR] Python was not found.
  echo [ERROR] Install Python 3.11 or 3.12 and enable "Add python.exe to PATH".
  echo [ERROR] Download: https://www.python.org/downloads/windows/
  start "" "https://www.python.org/downloads/windows/"
  pause
  exit /b 1
)

if exist "%SCRIPT_DIR%venv\Scripts\activate.bat" (
  call "%SCRIPT_DIR%venv\Scripts\activate.bat"
) else if exist "%SCRIPT_DIR%venv\Scripts\activate" (
  call "%SCRIPT_DIR%venv\Scripts\activate"
) else (
  echo [WARN] Python venv not found at "%SCRIPT_DIR%venv".
  echo [INFO] Creating venv and installing backend dependencies...
  call "%SCRIPT_DIR%install-deps.bat"
  if errorlevel 1 (
    echo [ERROR] Backend dependency installation failed. Check the log above.
    pause
    exit /b 1
  )
  call "%SCRIPT_DIR%venv\Scripts\activate.bat"
)

python -c "import customtkinter" >nul 2>nul
if errorlevel 1 (
  echo [WARN] customtkinter is missing. Reinstalling backend dependencies...
  call "%SCRIPT_DIR%install-deps.bat"
  if errorlevel 1 (
    echo [ERROR] Backend dependency installation failed. Check the log above.
    pause
    exit /b 1
  )
  call "%SCRIPT_DIR%venv\Scripts\activate.bat"
)

python "%SCRIPT_DIR%tk_launcher.py"
pause
