@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem Clean dependency installer for Windows.
rem Keeps a working system proxy (common in China / corporate networks).
rem Set HEYSURE_PIP_NO_PROXY=1 to force direct PyPI access (no proxy).
rem Set HEYSURE_PIP_PROXY=http://host:port to force a specific proxy.

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

rem Ignore user pip config files (broken index-url / cert settings).
set "PIP_CONFIG_FILE=NUL"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
set "PIP_NO_INPUT=1"

rem Proxy policy:
rem 1) HEYSURE_PIP_NO_PROXY=1  -> clear all proxy vars (direct)
rem 2) HEYSURE_PIP_PROXY=...   -> force that HTTP(S) proxy
rem 3) otherwise keep current process proxy env if present
if /I "%HEYSURE_PIP_NO_PROXY%"=="1" (
  echo [INFO] HEYSURE_PIP_NO_PROXY=1: installing without proxy.
  set "HTTP_PROXY="
  set "HTTPS_PROXY="
  set "ALL_PROXY="
  set "NO_PROXY="
  set "http_proxy="
  set "https_proxy="
  set "all_proxy="
  set "no_proxy="
) else if defined HEYSURE_PIP_PROXY (
  echo [INFO] Using HEYSURE_PIP_PROXY=%HEYSURE_PIP_PROXY%
  set "HTTP_PROXY=%HEYSURE_PIP_PROXY%"
  set "HTTPS_PROXY=%HEYSURE_PIP_PROXY%"
  set "http_proxy=%HEYSURE_PIP_PROXY%"
  set "https_proxy=%HEYSURE_PIP_PROXY%"
  set "ALL_PROXY="
  set "all_proxy="
) else (
  if defined HTTPS_PROXY (
    echo [INFO] Using existing HTTPS_PROXY for pip.
  ) else if defined https_proxy (
    echo [INFO] Using existing https_proxy for pip.
  ) else if defined HTTP_PROXY (
    echo [INFO] Using existing HTTP_PROXY for pip.
  ) else if defined http_proxy (
    echo [INFO] Using existing http_proxy for pip.
  ) else (
    echo [INFO] No proxy env detected; installing direct to PyPI.
  )
)

python -m pip install --upgrade pip --disable-pip-version-check
if errorlevel 1 (
  echo [WARN] Failed to upgrade pip. Continuing with the bundled pip version.
)

python -m pip install --isolated --no-cache-dir -r requirements.txt
if errorlevel 1 (
  echo [ERROR] Dependency installation failed.
  echo [HINT] If you use Clash/V2Ray/etc, keep the proxy running and ensure
  echo [HINT]   HTTP_PROXY/HTTPS_PROXY point to it, e.g. http://127.0.0.1:7897
  echo [HINT] Or set for this session:
  echo [HINT]   set HEYSURE_PIP_PROXY=http://127.0.0.1:7897
  echo [HINT]   call install-deps.bat
  echo [HINT] If a broken proxy is the problem:
  echo [HINT]   set HEYSURE_PIP_NO_PROXY=1
  echo [HINT]   call install-deps.bat
  exit /b 1
)

echo [SUCCESS] Dependencies installed successfully.
