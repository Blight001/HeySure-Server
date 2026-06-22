@echo off
setlocal EnableExtensions EnableDelayedExpansion

:: 设置编码为UTF-8，防止中文乱码
chcp 65001 >nul
title Port Release Tool

set "PORT=%~1"
if not defined PORT (
  set /p "PORT=请输入要释放的端口号: "
)

if not defined PORT (
  echo 未输入端口号，已退出。
  pause
  exit /b 1
)

:: 校验输入是否为纯数字
echo.%PORT%| findstr /R "^[0-9][0-9]*$" >nul
if errorlevel 1 (
  echo 端口号必须是数字。
  pause
  exit /b 1
)

set "PIDS="
:: 查找TCP协议占用的端口
for /f "tokens=5" %%P in ('netstat -ano -p tcp ^| findstr /R /C:":%PORT% " ^| findstr /I "LISTENING ESTABLISHED TIME_WAIT CLOSE_WAIT SYN_SENT SYN_RECEIVED FIN_WAIT_1 FIN_WAIT_2 LAST_ACK CLOSING"') do (
  call :AddPid %%P
)
:: 查找UDP协议占用的端口
for /f "tokens=4" %%P in ('netstat -ano -p udp ^| findstr /R /C:":%PORT% "') do (
  call :AddPid %%P
)

if not defined PIDS (
  echo 未找到占用端口 %PORT% 的进程。
  pause
  exit /b 0
)

echo.
echo 端口 %PORT% 的占用进程:
for %%P in (!PIDS!) do (
  call :ShowProcess %%P
)
echo.

set /p "CONFIRM=是否确认结束以上进程 [Y/N]: "
if /I not "%CONFIRM%"=="Y" (
  echo 已取消。
  pause
  exit /b 0
)

for %%P in (!PIDS!) do (
  echo 正在结束 PID %%P ...
  taskkill /F /PID %%P >nul 2>&1
  if errorlevel 1 (
    echo   失败，可能需要管理员权限或进程已退出。
  ) else (
    echo   已结束。
  )
)

echo.
echo 处理完成。
pause
exit /b 0

:ShowProcess
set "PID=%~1"
set "PROC_NAME="
for /f "usebackq tokens=1,2 delims=," %%A in (`tasklist /FI "PID eq %PID%" /FO CSV /NH 2^>nul`) do (
  if not "%%~B"=="" (
    set "PROC_NAME=%%~A"
    goto :ShowProcessDone
  )
)
:ShowProcessDone
if defined PROC_NAME echo 程序: !PROC_NAME!
echo PID: %PID%
goto :eof

:AddPid
set "NEWPID=%~1"
if not defined NEWPID goto :eof
if not defined PIDS (
  set "PIDS=%NEWPID%"
) else (
  echo !PIDS! | findstr /R /C:"\<%NEWPID%\>" >nul
  if errorlevel 1 set "PIDS=!PIDS! %NEWPID%"
)
goto :eof
