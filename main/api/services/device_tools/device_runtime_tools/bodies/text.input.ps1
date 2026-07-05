# device runtime tool: text.input
# 约定：对象 $toolArgs 已注入；把结果赋给 $result。由 powershell-runner 在设备上执行（Windows PowerShell 5.1 兼容）。
# 此文件是该工具的默认实现（真相源）；AI 可经 device_mcp.manage（action=upsert 同名工具）改写实例。
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -Namespace HS -Name Kbd -MemberDefinition @'
[DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
'@
$text = [string]$toolArgs.text
if ([string]::IsNullOrEmpty($text)) {
    [System.Windows.Forms.Clipboard]::Clear()
} else {
    [System.Windows.Forms.Clipboard]::SetText($text)
}
$paste = $true
if ($null -ne $toolArgs.paste) { $paste = [bool]$toolArgs.paste }
if ($paste) {
    # Ctrl+V（0x11 = VK_CONTROL，0x56 = 'V'；2 = KEYEVENTF_KEYUP）
    [HS.Kbd]::keybd_event(0x11, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 20
    [HS.Kbd]::keybd_event(0x56, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 20
    [HS.Kbd]::keybd_event(0x56, 0, 2, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 20
    [HS.Kbd]::keybd_event(0x11, 0, 2, [UIntPtr]::Zero)
}
$result = @{ ok = $true }
