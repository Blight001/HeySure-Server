# device runtime tool: ui.inspect
# 约定：对象 $toolArgs 已注入；把结果赋给 $result。由 powershell-runner 在设备上执行（Windows PowerShell 5.1 兼容）。
# 此文件是该工具的默认实现（真相源）；AI 可经 device_mcp.manage（action=upsert 同名工具）改写实例。
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -Namespace HS -Name Fg -MemberDefinition '[DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();'

# ControlType.ProgrammaticName 形如 "ControlType.Button" → 输出 "ButtonControl"，
# 与 Python uiautomation 的 ControlTypeName 命名保持一致。
function Get-ControlTypeName($el) {
    $ptn = $el.Current.ControlType.ProgrammaticName
    return ($ptn -replace '^ControlType\.', '') + 'Control'
}

function Find-WindowByTitle([string]$title) {
    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
    $rootEl = [System.Windows.Automation.AutomationElement]::RootElement
    # 与 Python 版 WindowControl(searchDepth=2, SubName=...) 一致：搜到第二层。
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
    $hwnd = [HS.Fg]::GetForegroundWindow()
    $root = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
}

$limit = 150
if ($toolArgs.max) { $limit = [int]$toolArgs.max }
$maxDepth = 8
if ($toolArgs.max_depth) { $maxDepth = [int]$toolArgs.max_depth }

$elems = New-Object System.Collections.Generic.List[object]
$walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
# 深度优先遍历（栈中保存 元素,深度），与 Python WalkControl 的顺序一致。
$stack = New-Object System.Collections.Generic.Stack[object]
$first = $walker.GetFirstChild($root)
$startChildren = New-Object System.Collections.Generic.List[object]
$node = $first
while ($null -ne $node) {
    $startChildren.Add($node)
    $node = $walker.GetNextSibling($node)
}
for ($i = $startChildren.Count - 1; $i -ge 0; $i--) {
    $stack.Push(@($startChildren[$i], 1))
}
while (($stack.Count -gt 0) -and ($elems.Count -lt $limit)) {
    $pair = $stack.Pop()
    $el = $pair[0]
    $depth = [int]$pair[1]
    try {
        $r = $el.Current.BoundingRectangle
        if ($r.IsEmpty) { $rect = @(0, 0, 0, 0) } else { $rect = @([int]$r.Left, [int]$r.Top, [int]$r.Right, [int]$r.Bottom) }
        $elems.Add(@{
            name          = [string]$el.Current.Name
            control_type  = (Get-ControlTypeName $el)
            automation_id = [string]$el.Current.AutomationId
            rect          = $rect
        })
    } catch { continue }
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
$result = @{ window = [string]$root.Current.Name; elements = $elems }
