# device runtime tool: ui.click
# 约定：对象 $toolArgs 已注入；把结果赋给 $result。由 powershell-runner 在设备上执行（Windows PowerShell 5.1 兼容）。
# 此文件是该工具的默认实现（真相源）；AI 可经 device_mcp.manage（action=upsert 同名工具）改写实例。
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -Namespace HS -Name Native -MemberDefinition @'
[DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
[DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
[DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, int dx, int dy, int dwData, UIntPtr dwExtraInfo);
'@
[void][HS.Native]::SetProcessDPIAware()

function Get-ControlTypeName($el) {
    $ptn = $el.Current.ControlType.ProgrammaticName
    return ($ptn -replace '^ControlType\.', '') + 'Control'
}

function Find-WindowByTitle([string]$title) {
    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
    $rootEl = [System.Windows.Automation.AutomationElement]::RootElement
    $level1 = @()
    $child = $walker.GetFirstChild($rootEl)
    while ($null -ne $child) {
        $level1 += $child
        $child = $walker.GetNextSibling($child)
    }
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
    $root = [System.Windows.Automation.AutomationElement]::FromHandle([HS.Native]::GetForegroundWindow())
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
while ($null -ne $node) {
    $startChildren.Add($node)
    $node = $walker.GetNextSibling($node)
}
for ($i = $startChildren.Count - 1; $i -ge 0; $i--) {
    $stack.Push(@($startChildren[$i], 1))
}
while ($stack.Count -gt 0) {
    $pair = $stack.Pop()
    $el = $pair[0]
    $depth = [int]$pair[1]
    $matched = $true
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
            while ($null -ne $c) {
                $children.Add($c)
                $c = $walker.GetNextSibling($c)
            }
        } catch {}
        for ($i = $children.Count - 1; $i -ge 0; $i--) {
            $stack.Push(@($children[$i], $depth + 1))
        }
    }
}
if ($null -eq $target) { throw 'control not found' }

# 优先 InvokePattern；不支持时真实点击控件中心（与 Python 版一致）。
$invoked = $false
try {
    $pattern = $target.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
    if ($pattern) {
        $pattern.Invoke()
        $invoked = $true
    }
} catch {}
if (-not $invoked) {
    $r = $target.Current.BoundingRectangle
    if ($r.IsEmpty) { throw 'control has no clickable area' }
    $cx = [int](($r.Left + $r.Right) / 2)
    $cy = [int](($r.Top + $r.Bottom) / 2)
    [void][HS.Native]::SetCursorPos($cx, $cy)
    Start-Sleep -Milliseconds 10
    [HS.Native]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 10
    [HS.Native]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
}
$result = @{ ok = $true; name = [string]$target.Current.Name }
