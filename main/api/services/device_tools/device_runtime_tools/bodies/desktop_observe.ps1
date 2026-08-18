# device runtime tool: desktop_observe
# $toolArgs is injected; assign output to $result. Windows PowerShell 5.1.
$ErrorActionPreference = 'Stop'

$action = ([string]$toolArgs.action).ToLower()
if (-not $action) { $action = 'ui' }

if ($action -eq 'displays') {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -Namespace HS -Name Dpi -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();'
    [void][HS.Dpi]::SetProcessDPIAware()
    $monitors = New-Object System.Collections.Generic.List[object]
    $i = 0
    foreach ($s in [System.Windows.Forms.Screen]::AllScreens) {
        $b = $s.Bounds
        $monitors.Add(@{ index = $i; left = $b.Left; top = $b.Top; width = $b.Width; height = $b.Height })
        $i++
    }
    $result = @{ action = 'displays'; monitors = $monitors }
} elseif ($action -eq 'windows') {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
namespace HS {
    public static class Win {
        public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
        [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lParam);
        [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
        [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr hWnd);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);
        public static List<string> VisibleTitles() {
            var list = new List<string>();
            EnumWindows(delegate(IntPtr h, IntPtr l) {
                if (!IsWindowVisible(h)) return true;
                int len = GetWindowTextLength(h);
                if (len <= 0) return true;
                var sb = new StringBuilder(len + 1);
                GetWindowText(h, sb, len + 1);
                string t = sb.ToString();
                if (t.Trim().Length > 0) list.Add(t);
                return true;
            }, IntPtr.Zero);
            return list;
        }
    }
}
'@
    $result = @{ action = 'windows'; windows = [HS.Win]::VisibleTitles() }
} elseif ($action -eq 'ui') {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    Add-Type -Namespace HS -Name Fg -MemberDefinition '[DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();'

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
        $root = [System.Windows.Automation.AutomationElement]::FromHandle([HS.Fg]::GetForegroundWindow())
    }

    $limit = 150
    if ($toolArgs.max) { $limit = [int]$toolArgs.max }
    $maxDepth = 8
    if ($toolArgs.max_depth) { $maxDepth = [int]$toolArgs.max_depth }

    $elems = New-Object System.Collections.Generic.List[object]
    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
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
    $result = @{ action = 'ui'; window = [string]$root.Current.Name; elements = $elems }
} else {
    throw "unsupported action: $action"
}
