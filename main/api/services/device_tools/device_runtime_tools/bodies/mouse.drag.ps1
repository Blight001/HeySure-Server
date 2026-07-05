# device runtime tool: mouse.drag
# 约定：对象 $toolArgs 已注入；把结果赋给 $result。由 powershell-runner 在设备上执行（Windows PowerShell 5.1 兼容）。
# 此文件是该工具的默认实现（真相源）；AI 可经 device_mcp.manage（action=upsert 同名工具）改写实例。
$ErrorActionPreference = 'Stop'
Add-Type -Namespace HS -Name Mouse -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
[DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
[DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, int dx, int dy, int dwData, UIntPtr dwExtraInfo);
'@
foreach ($k in 'x1', 'y1', 'x2', 'y2') {
    if ($null -eq $toolArgs.$k) { throw "$k is required" }
}
$x1 = [int]$toolArgs.x1; $y1 = [int]$toolArgs.y1
$x2 = [int]$toolArgs.x2; $y2 = [int]$toolArgs.y2
[void][HS.Mouse]::SetProcessDPIAware()
[void][HS.Mouse]::SetCursorPos($x1, $y1)
Start-Sleep -Milliseconds 30
[HS.Mouse]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
# 约 200ms 内插值移动，与 Python 版 dragTo(duration=0.2) 一致。
$steps = 12
for ($i = 1; $i -le $steps; $i++) {
    $nx = [int]($x1 + ($x2 - $x1) * $i / $steps)
    $ny = [int]($y1 + ($y2 - $y1) * $i / $steps)
    [void][HS.Mouse]::SetCursorPos($nx, $ny)
    Start-Sleep -Milliseconds 16
}
[void][HS.Mouse]::SetCursorPos($x2, $y2)
[HS.Mouse]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
$result = @{ ok = $true }
