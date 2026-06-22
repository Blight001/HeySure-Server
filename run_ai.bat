@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem Load repository root .env so the AI worker sees the same database and
rem internal service URLs as the rest of the stack.
set "ROOT_DIR=%~dp0.."
set "ENV_FILE=%ROOT_DIR%\.env"

if exist "%ENV_FILE%" (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%ENV_FILE%") do (
    if not "%%A"=="" set "%%A=%%B"
  )
)

cd /d "%~dp0"
set "PYTHONPATH=%~dp0main;%~dp0"

if exist "venv\Scripts\activate.bat" (
  call "venv\Scripts\activate.bat"
) else if exist "venv\Scripts\activate" (
  call "venv\Scripts\activate"
) else (
  echo [WARN] Python venv not found at "%~dp0venv". Continuing with the current Python interpreter.
)

if not defined DATABASE_URL (
  echo [ERROR] DATABASE_URL is missing.
  echo [ERROR] Copy ".env.example" to ".env" at the repository root and set DATABASE_URL before starting the server.
  exit /b 1
)

if not defined HEYSURE_API_GATEWAY_URL set "HEYSURE_API_GATEWAY_URL=http://127.0.0.1:3000"
if not defined MCP_RUNTIME_URL set "MCP_RUNTIME_URL=http://127.0.0.1:3001"
if not defined CONNECTOR_RUNTIME_URL set "CONNECTOR_RUNTIME_URL=http://127.0.0.1:3002"
if not defined HEYSURE_AI_DEBUG set "HEYSURE_AI_DEBUG=1"

python -m ai_runtime.main
