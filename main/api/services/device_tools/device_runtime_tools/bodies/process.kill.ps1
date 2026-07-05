# device runtime tool: process.kill
# 约定：对象 $toolArgs 已注入；把结果赋给 $result。由 powershell-runner 在设备上执行（Windows PowerShell 5.1 兼容）。
# 此文件是该工具的默认实现（真相源）；AI 可经 device_mcp.manage（action=upsert 同名工具）改写实例。
$ErrorActionPreference = 'Stop'
if ($null -eq $toolArgs.pid) { throw 'pid is required' }
$procId = [int]$toolArgs.pid
Stop-Process -Id $procId -ErrorAction Stop
$result = @{ ok = $true; pid = $procId }
