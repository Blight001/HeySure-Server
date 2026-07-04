# device runtime tool: window.focus
# 约定：对象 $toolArgs 已注入；把结果赋给 $result。由 powershell-runner 在设备上执行（Windows PowerShell 5.1 兼容）。
# 此文件是该工具的默认实现（真相源）；AI 可经 mcp.manage_dynamic_tool 改写实例。
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
namespace HS {
    public static class Win {
        public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
        [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lParam);
        [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr hWnd);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);
        [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
        [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
        [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
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
}
'@
$title = [string]$toolArgs.title
if (-not $title) { throw 'title is required' }
$target = [IntPtr]::Zero
foreach ($h in [HS.Win]::Handles()) {
    $t = [HS.Win]::TitleOf($h)
    if ($t -and ($t.IndexOf($title, [StringComparison]::OrdinalIgnoreCase) -ge 0)) { $target = $h; break }
}
if ($target -eq [IntPtr]::Zero) { throw "window not found: $title" }
if ([HS.Win]::IsIconic($target)) { [void][HS.Win]::ShowWindow($target, 9) }  # SW_RESTORE
[void][HS.Win]::SetForegroundWindow($target)
$result = @{ ok = $true; title = $title }
