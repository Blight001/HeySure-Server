# device runtime tool: keyboard.press
# 约定：对象 $toolArgs 已注入；把结果赋给 $result。由 powershell-runner 在设备上执行（Windows PowerShell 5.1 兼容）。
# 此文件是该工具的默认实现（真相源）；AI 可经 device_mcp.manage（action=upsert 同名工具）改写实例。
$ErrorActionPreference = 'Stop'
Add-Type -Namespace HS -Name Kbd -MemberDefinition @'
[DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
[DllImport("user32.dll")] public static extern short VkKeyScan(char ch);
'@

$keys = $toolArgs.keys
if ($keys -is [string]) {
    $keys = @($keys.Replace('+', ' ').Split(' ') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}
$keys = @(@($keys) | ForEach-Object { ([string]$_).Trim().ToLower() } | Where-Object { $_ })
if ($keys.Count -eq 0) { throw 'keys is required' }

$VK = @{
    ctrl = 0x11; control = 0x11; alt = 0x12; menu = 0x12; shift = 0x10
    win = 0x5B; winleft = 0x5B; winright = 0x5C; apps = 0x5D
    enter = 0x0D; return = 0x0D; esc = 0x1B; escape = 0x1B; tab = 0x09; space = 0x20
    backspace = 0x08; delete = 0x2E; del = 0x2E; insert = 0x2D
    home = 0x24; end = 0x23; pageup = 0x21; pgup = 0x21; pagedown = 0x22; pgdn = 0x22
    up = 0x26; down = 0x28; left = 0x25; right = 0x27
    capslock = 0x14; numlock = 0x90; printscreen = 0x2C; prtsc = 0x2C; pause = 0x13
}

function Resolve-VK([string]$k) {
    if ($VK.ContainsKey($k)) { return [byte]$VK[$k] }
    if ($k -match '^f([1-9]|1[0-9]|2[0-4])$') { return [byte](0x6F + [int]$Matches[1]) }
    if ($k.Length -eq 1) {
        $code = [HS.Kbd]::VkKeyScan([char]$k)
        if ($code -ne -1) { return [byte]($code -band 0xFF) }
    }
    throw "unsupported key: $k"
}

$vks = @($keys | ForEach-Object { Resolve-VK $_ })
# 与 pyautogui.hotkey 一致：按顺序全部按下，再逆序释放。
foreach ($vk in $vks) {
    [HS.Kbd]::keybd_event($vk, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 20
}
[array]::Reverse($vks)
foreach ($vk in $vks) {
    [HS.Kbd]::keybd_event($vk, 0, 2, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 20
}
$result = @{ ok = $true; keys = $keys }
