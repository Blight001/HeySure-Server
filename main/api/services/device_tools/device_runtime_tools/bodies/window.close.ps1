# device runtime tool: window.close
# 约定：对象 $toolArgs 已注入；把结果赋给 $result。由 powershell-runner 在设备上执行（Windows PowerShell 5.1 兼容）。
# 此文件是该工具的默认实现（真相源）；AI 可经 device_mcp.manage（action=upsert 同名工具）改写实例。
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
[void][HS.Win]::PostMessage($target, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero)  # WM_CLOSE
$result = @{ ok = $true; title = $title }
