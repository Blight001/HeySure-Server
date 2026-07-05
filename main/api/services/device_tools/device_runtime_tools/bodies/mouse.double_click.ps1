# device runtime tool: mouse.double_click
# 约定：对象 $toolArgs 已注入；把结果赋给 $result。由 powershell-runner 在设备上执行（Windows PowerShell 5.1 兼容）。
# 此文件是该工具的默认实现（真相源）；AI 可经 device_mcp.manage（action=upsert 同名工具）改写实例。
$ErrorActionPreference = 'Stop'
Add-Type -Namespace HS -Name Mouse -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
[DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
[DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, int dx, int dy, int dwData, UIntPtr dwExtraInfo);
'@
[void][HS.Mouse]::SetProcessDPIAware()
if (($null -ne $toolArgs.x) -and ($null -ne $toolArgs.y)) {
    [void][HS.Mouse]::SetCursorPos([int]$toolArgs.x, [int]$toolArgs.y)
    Start-Sleep -Milliseconds 10
}
for ($i = 0; $i -lt 2; $i++) {
    [HS.Mouse]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 10
    [HS.Mouse]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 10
}
$result = @{ ok = $true }
