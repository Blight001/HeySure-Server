# device runtime tool: fs.read
# 约定：对象 $toolArgs 已注入；把结果赋给 $result。由 powershell-runner 在设备上执行（Windows PowerShell 5.1 兼容）。
# 此文件是该工具的默认实现（真相源）；AI 可经 mcp.manage_dynamic_tool 改写实例。
$ErrorActionPreference = 'Stop'
$rel = [string]$toolArgs.path
if (-not $rel) { throw 'path is required' }
if ([System.IO.Path]::IsPathRooted($rel)) { $p = $rel } else { $p = Join-Path (Get-Location).Path $rel }
$cap = 200000
if ($toolArgs.maxBytes) { $cap = [int]$toolArgs.maxBytes }
if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { throw "file not found: $p" }
# 与 Python 版一致：最多读 cap+1 个字符，超出即标记 truncated（UTF-8，坏字节替换）。
$reader = New-Object System.IO.StreamReader($p, $true)
try {
    $buf = New-Object char[] ($cap + 1)
    $total = 0
    while ($total -lt $buf.Length) {
        $n = $reader.Read($buf, $total, $buf.Length - $total)
        if ($n -le 0) { break }
        $total += $n
    }
} finally {
    $reader.Close()
}
$content = New-Object string($buf, 0, [Math]::Min($total, $cap))
$result = @{ path = $p; content = $content; truncated = ($total -gt $cap) }
