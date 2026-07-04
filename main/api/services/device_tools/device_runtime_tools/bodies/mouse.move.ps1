# device runtime tool: mouse.move
# 约定：对象 $toolArgs 已注入；把结果赋给 $result。由 powershell-runner 在设备上执行（Windows PowerShell 5.1 兼容）。
# 此文件是该工具的默认实现（真相源）；AI 可经 mcp.manage_dynamic_tool 改写实例。
$ErrorActionPreference = 'Stop'
Add-Type -Namespace HS -Name Mouse -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
[DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
'@
if (($null -eq $toolArgs.x) -or ($null -eq $toolArgs.y)) { throw 'x and y are required' }
[void][HS.Mouse]::SetProcessDPIAware()
[void][HS.Mouse]::SetCursorPos([int]$toolArgs.x, [int]$toolArgs.y)
$result = @{ ok = $true }
