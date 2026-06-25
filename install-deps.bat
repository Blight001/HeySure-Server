@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem Clean dependency installer for Windows.
rem It ignores user pip config and proxy settings so a broken proxy does not
rem trigger TLS hostname errors during pip resolution.

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

set "PYTHON_CMD="
where python >nul 2>nul && set "PYTHON_CMD=python"
if not defined PYTHON_CMD (
  where py >nul 2>nul && set "PYTHON_CMD=py -3"
)
if not defined PYTHON_CMD (
  echo [ERROR] Python is not installed or not available in PATH.
  echo [HINT] Install Python 3.11 or 3.12 from:
  echo [HINT] https://www.python.org/downloads/windows/
  start "" "https://www.python.org/downloads/windows/"
  exit /b 1
)

if not exist "venv" (
  %PYTHON_CMD% -m venv venv
  if errorlevel 1 (
    echo [ERROR] Failed to create virtual environment.
    exit /b 1
  )
)

call "venv\Scripts\activate.bat"

rem Disable proxy inheritance for this install session.
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "ALL_PROXY="
set "NO_PROXY="
set "http_proxy="
set "https_proxy="
set "all_proxy="
set "no_proxy="

rem Ignore user pip config files and environment overrides.
set "PIP_CONFIG_FILE=NUL"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
set "PIP_NO_INPUT=1"

python -m pip install --upgrade pip --disable-pip-version-check
if errorlevel 1 (
  echo [WARN] Failed to upgrade pip. Continuing with the bundled pip version.
)

python -m pip install --isolated --no-cache-dir -r requirements.txt
if errorlevel 1 (
  echo [ERROR] Dependency installation failed.
  echo [HINT] If you must use a proxy, configure it explicitly for this session.
  echo [HINT] Otherwise keep proxy variables empty and retry.
  exit /b 1
)

echo [SUCCESS] Dependencies installed successfully.
