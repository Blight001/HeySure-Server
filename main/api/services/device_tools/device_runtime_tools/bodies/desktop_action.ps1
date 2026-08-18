# device runtime tool: desktop_action
# $toolArgs is injected; assign output to $result. Windows PowerShell 5.1.
$ErrorActionPreference = 'Stop'

$action = ([string]$toolArgs.action).ToLower()
if (-not $action) { throw 'action is required' }

Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
namespace HS {
    public static class Native {
        public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
        [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
        [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
        [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, int dx, int dy, int dwData, UIntPtr dwExtraInfo);
        [DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
        [DllImport("user32.dll")] public static extern short VkKeyScan(char ch);
        [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lParam);
        [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr hWnd);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);
        [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
        [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
        [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern bool PostMessage(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);
        public static List<IntPtr> Handles() {
            var list = new List<IntPtr>();
            EnumWindows(delegate(IntPtr h, IntPtr l) { list.Add(h); return true; }, IntPtr.Zero);
            return list;
        }
        public static string TitleOf(IntPtr h) {
            int len = GetWindowTextLength(h);
            if (len <= 0) return "";
            var sb = new StringBuilder(len + 1);
            GetWindowText(h, sb, len + 1);
            return sb.ToString();
        }
    }
    public static class Kbd {
        [StructLayout(LayoutKind.Sequential)]
        public struct KEYBDINPUT { public ushort wVk; public ushort wScan; public uint dwFlags; public uint time; public UIntPtr dwExtraInfo; }
        [StructLayout(LayoutKind.Sequential)]
        public struct MOUSEINPUT { public int dx; public int dy; public uint mouseData; public uint dwFlags; public uint time; public UIntPtr dwExtraInfo; }
        [StructLayout(LayoutKind.Explicit)]
        public struct UNION { [FieldOffset(0)] public MOUSEINPUT mi; [FieldOffset(0)] public KEYBDINPUT ki; }
        [StructLayout(LayoutKind.Sequential)]
        public struct INPUT { public uint type; public UNION U; }
        [DllImport("user32.dll", SetLastError = true)]
        public static extern uint SendInput(uint nInputs, INPUT[] pInputs, int cbSize);
        const uint KEYEVENTF_KEYUP = 0x0002, KEYEVENTF_UNICODE = 0x0004;
        const ushort VK_RETURN = 0x0D, VK_TAB = 0x09;
        static void Tap(ushort vk) {
            INPUT[] inp = new INPUT[2];
            inp[0].type = 1; inp[0].U.ki.wVk = vk;
            inp[1].type = 1; inp[1].U.ki.wVk = vk; inp[1].U.ki.dwFlags = KEYEVENTF_KEYUP;
            SendInput(2, inp, Marshal.SizeOf(typeof(INPUT)));
        }
        public static void TypeText(string text, int delayMs) {
            foreach (char c in text) {
                if (c == '\r') continue;
                if (c == '\n') { Tap(VK_RETURN); }
                else if (c == '\t') { Tap(VK_TAB); }
                else {
                    INPUT[] inp = new INPUT[2];
                    inp[0].type = 1; inp[0].U.ki.wScan = c; inp[0].U.ki.dwFlags = KEYEVENTF_UNICODE;
                    inp[1].type = 1; inp[1].U.ki.wScan = c; inp[1].U.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP;
                    SendInput(2, inp, Marshal.SizeOf(typeof(INPUT)));
                }
                if (delayMs > 0) System.Threading.Thread.Sleep(delayMs);
            }
        }
    }
}
'@

function Find-WindowHandle([string]$title) {
    if (-not $title) { throw 'title is required' }
    foreach ($h in [HS.Native]::Handles()) {
        $t = [HS.Native]::TitleOf($h)
        if ($t -and ($t.IndexOf($title, [StringComparison]::OrdinalIgnoreCase) -ge 0)) { return $h }
    }
    throw "window not found: $title"
}

function Invoke-MouseClick([string]$button, [int]$times) {
    $flags = @{ left = @(0x0002, 0x0004); right = @(0x0008, 0x0010); middle = @(0x0020, 0x0040) }
    $btn = if ($button) { $button.ToLower() } else { 'left' }
    if (-not $flags.ContainsKey($btn)) { throw "unsupported button: $btn" }
    [void][HS.Native]::SetProcessDPIAware()
    if (($null -ne $toolArgs.x) -and ($null -ne $toolArgs.y)) {
        [void][HS.Native]::SetCursorPos([int]$toolArgs.x, [int]$toolArgs.y)
        Start-Sleep -Milliseconds 10
    }
    for ($i = 0; $i -lt $times; $i++) {
        [HS.Native]::mouse_event($flags[$btn][0], 0, 0, 0, [UIntPtr]::Zero)
        Start-Sleep -Milliseconds 10
        [HS.Native]::mouse_event($flags[$btn][1], 0, 0, 0, [UIntPtr]::Zero)
        Start-Sleep -Milliseconds 10
    }
}

if ($action -eq 'move') {
    if (($null -eq $toolArgs.x) -or ($null -eq $toolArgs.y)) { throw 'x and y are required' }
    [void][HS.Native]::SetProcessDPIAware()
    [void][HS.Native]::SetCursorPos([int]$toolArgs.x, [int]$toolArgs.y)
    $result = @{ ok = $true; action = $action }
} elseif ($action -eq 'click') {
    Invoke-MouseClick ([string]$toolArgs.button) 1
    $result = @{ ok = $true; action = $action }
} elseif ($action -eq 'double_click') {
    Invoke-MouseClick 'left' 2
    $result = @{ ok = $true; action = $action }
} elseif ($action -eq 'right_click') {
    Invoke-MouseClick 'right' 1
    $result = @{ ok = $true; action = $action }
} elseif ($action -eq 'scroll') {
    [HS.Native]::mouse_event(0x0800, 0, 0, [int]$toolArgs.amount, [UIntPtr]::Zero)
    $result = @{ ok = $true; action = $action }
} elseif ($action -eq 'drag') {
    foreach ($k in 'x1', 'y1', 'x2', 'y2') { if ($null -eq $toolArgs.$k) { throw "$k is required" } }
    $x1 = [int]$toolArgs.x1; $y1 = [int]$toolArgs.y1
    $x2 = [int]$toolArgs.x2; $y2 = [int]$toolArgs.y2
    [void][HS.Native]::SetProcessDPIAware()
    [void][HS.Native]::SetCursorPos($x1, $y1)
    Start-Sleep -Milliseconds 30
    [HS.Native]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
    for ($i = 1; $i -le 12; $i++) {
        [void][HS.Native]::SetCursorPos([int]($x1 + ($x2 - $x1) * $i / 12), [int]($y1 + ($y2 - $y1) * $i / 12))
        Start-Sleep -Milliseconds 16
    }
    [void][HS.Native]::SetCursorPos($x2, $y2)
    [HS.Native]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
    $result = @{ ok = $true; action = $action }
} elseif ($action -eq 'type') {
    [HS.Kbd]::TypeText([string]$toolArgs.text, 10)
    $result = @{ ok = $true; action = $action }
} elseif ($action -eq 'press') {
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
    $vks = @()
    foreach ($k in $keys) {
        if ($VK.ContainsKey($k)) { $vks += [byte]$VK[$k]; continue }
        if ($k -match '^f([1-9]|1[0-9]|2[0-4])$') { $vks += [byte](0x6F + [int]$Matches[1]); continue }
        if ($k.Length -eq 1) {
            $code = [HS.Native]::VkKeyScan([char]$k)
            if ($code -ne -1) { $vks += [byte]($code -band 0xFF); continue }
        }
        throw "unsupported key: $k"
    }
    foreach ($vk in $vks) { [HS.Native]::keybd_event($vk, 0, 0, [UIntPtr]::Zero); Start-Sleep -Milliseconds 20 }
    [array]::Reverse($vks)
    foreach ($vk in $vks) { [HS.Native]::keybd_event($vk, 0, 2, [UIntPtr]::Zero); Start-Sleep -Milliseconds 20 }
    $result = @{ ok = $true; action = $action; keys = $keys }
} elseif ($action -eq 'paste') {
    Add-Type -AssemblyName System.Windows.Forms
    $text = [string]$toolArgs.text
    if ([string]::IsNullOrEmpty($text)) { [System.Windows.Forms.Clipboard]::Clear() }
    else { [System.Windows.Forms.Clipboard]::SetText($text) }
    [HS.Native]::keybd_event(0x11, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 20
    [HS.Native]::keybd_event(0x56, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 20
    [HS.Native]::keybd_event(0x56, 0, 2, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 20
    [HS.Native]::keybd_event(0x11, 0, 2, [UIntPtr]::Zero)
    $result = @{ ok = $true; action = $action }
} elseif ($action -eq 'focus') {
    $target = Find-WindowHandle ([string]$toolArgs.title)
    if ([HS.Native]::IsIconic($target)) { [void][HS.Native]::ShowWindow($target, 9) }
    [void][HS.Native]::SetForegroundWindow($target)
    $result = @{ ok = $true; action = $action; title = [string]$toolArgs.title }
} elseif ($action -eq 'close') {
    $target = Find-WindowHandle ([string]$toolArgs.title)
    [void][HS.Native]::PostMessage($target, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero)
    $result = @{ ok = $true; action = $action; title = [string]$toolArgs.title }
} elseif ($action -eq 'ui_click') {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    Add-Type -Namespace HS -Name Fg -MemberDefinition '[DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();'
    function Get-ControlTypeName($el) {
        return ($el.Current.ControlType.ProgrammaticName -replace '^ControlType\.', '') + 'Control'
    }
    function Find-WindowByTitle([string]$title) {
        $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
        $rootEl = [System.Windows.Automation.AutomationElement]::RootElement
        $level1 = @()
        $child = $walker.GetFirstChild($rootEl)
        while ($null -ne $child) { $level1 += $child; $child = $walker.GetNextSibling($child) }
        foreach ($el in $level1) {
            try { if (([string]$el.Current.Name).IndexOf($title, [StringComparison]::OrdinalIgnoreCase) -ge 0) { return $el } } catch {}
        }
        foreach ($el in $level1) {
            $sub = $walker.GetFirstChild($el)
            while ($null -ne $sub) {
                try { if (([string]$sub.Current.Name).IndexOf($title, [StringComparison]::OrdinalIgnoreCase) -ge 0) { return $sub } } catch {}
                $sub = $walker.GetNextSibling($sub)
            }
        }
        return $null
    }
    $title = [string]$toolArgs.title
    if ($title) {
        $root = Find-WindowByTitle $title
        if ($null -eq $root) { throw "window not found: $title" }
    } else {
        $root = [System.Windows.Automation.AutomationElement]::FromHandle([HS.Fg]::GetForegroundWindow())
    }
    $name = [string]$toolArgs.name
    $aid = [string]$toolArgs.automation_id
    $ctype = [string]$toolArgs.control_type
    $maxDepth = 8
    if ($toolArgs.max_depth) { $maxDepth = [int]$toolArgs.max_depth }
    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
    $target = $null
    $stack = New-Object System.Collections.Generic.Stack[object]
    $startChildren = New-Object System.Collections.Generic.List[object]
    $node = $walker.GetFirstChild($root)
    while ($null -ne $node) { $startChildren.Add($node); $node = $walker.GetNextSibling($node) }
    for ($i = $startChildren.Count - 1; $i -ge 0; $i--) { $stack.Push(@($startChildren[$i], 1)) }
    while ($stack.Count -gt 0) {
        $pair = $stack.Pop(); $el = $pair[0]; $depth = [int]$pair[1]; $matched = $true
        try {
            if ($aid -and (([string]$el.Current.AutomationId) -ne $aid)) { $matched = $false }
            if ($matched -and $name -and (([string]$el.Current.Name).IndexOf($name) -lt 0)) { $matched = $false }
            if ($matched -and $ctype -and ((Get-ControlTypeName $el) -ne $ctype)) { $matched = $false }
        } catch { $matched = $false }
        if ($matched -and ($aid -or $name -or $ctype)) { $target = $el; break }
        if ($depth -lt $maxDepth) {
            $children = New-Object System.Collections.Generic.List[object]
            try {
                $c = $walker.GetFirstChild($el)
                while ($null -ne $c) { $children.Add($c); $c = $walker.GetNextSibling($c) }
            } catch {}
            for ($i = $children.Count - 1; $i -ge 0; $i--) { $stack.Push(@($children[$i], $depth + 1)) }
        }
    }
    if ($null -eq $target) { throw 'control not found' }
    $invoked = $false
    try {
        $pattern = $target.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
        if ($pattern) { $pattern.Invoke(); $invoked = $true }
    } catch {}
    if (-not $invoked) {
        $r = $target.Current.BoundingRectangle
        if ($r.IsEmpty) { throw 'control has no clickable area' }
        [void][HS.Native]::SetProcessDPIAware()
        [void][HS.Native]::SetCursorPos([int](($r.Left + $r.Right) / 2), [int](($r.Top + $r.Bottom) / 2))
        Start-Sleep -Milliseconds 10
        [HS.Native]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
        Start-Sleep -Milliseconds 10
        [HS.Native]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
    }
    $result = @{ ok = $true; action = $action; name = [string]$target.Current.Name }
} else {
    throw "unsupported action: $action"
}
