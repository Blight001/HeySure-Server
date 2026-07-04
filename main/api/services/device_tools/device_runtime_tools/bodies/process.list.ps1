# device runtime tool: process.list
# 约定：对象 $toolArgs 已注入；把结果赋给 $result。由 powershell-runner 在设备上执行（Windows PowerShell 5.1 兼容）。
# 此文件是该工具的默认实现（真相源）；AI 可经 mcp.manage_dynamic_tool 改写实例。
$ErrorActionPreference = 'Stop'
$needle = ([string]$toolArgs.name_contains).ToLower()
$rows = New-Object System.Collections.Generic.List[object]
foreach ($p in (Get-Process)) {
    $name = $p.ProcessName
    if ($needle -and (-not $name.ToLower().Contains($needle))) { continue }
    $rows.Add(@{ pid = $p.Id; name = $name })
}
$capped = New-Object System.Collections.Generic.List[object]
foreach ($row in $rows) {
    if ($capped.Count -ge 500) { break }
    $capped.Add($row)
}
$result = @{ processes = $capped; count = $rows.Count }
