# device runtime tool: mouse.scroll
# 约定：对象 $toolArgs 已注入；把结果赋给 $result。由 powershell-runner 在设备上执行（Windows PowerShell 5.1 兼容）。
# 此文件是该工具的默认实现（真相源）；AI 可经 device_mcp.manage（action=upsert 同名工具）改写实例。
$ErrorActionPreference = 'Stop'
Add-Type -Namespace HS -Name Mouse -MemberDefinition @'
[DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, int dx, int dy, int dwData, UIntPtr dwExtraInfo);
'@
# dwData 直接使用 amount 原始值，与 pyautogui.scroll 在 Windows 上的行为一致
# （120 = 一格滚轮；正为上、负为下）。
$amount = [int]$toolArgs.amount
[HS.Mouse]::mouse_event(0x0800, 0, 0, $amount, [UIntPtr]::Zero)
$result = @{ ok = $true }
