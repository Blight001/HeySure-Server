# device runtime tool: fs.write
# 约定：对象 $toolArgs 已注入；把结果赋给 $result。由 powershell-runner 在设备上执行（Windows PowerShell 5.1 兼容）。
# 此文件是该工具的默认实现（真相源）；AI 可经 mcp.manage_dynamic_tool 改写实例。
$ErrorActionPreference = 'Stop'
$rel = [string]$toolArgs.path
if (-not $rel) { throw 'path is required' }
if ([System.IO.Path]::IsPathRooted($rel)) { $p = $rel } else { $p = Join-Path (Get-Location).Path $rel }
$dir = Split-Path -Parent $p
if ($dir) { [void][System.IO.Directory]::CreateDirectory($dir) }
$content = [string]$toolArgs.content
# UTF-8 无 BOM，与 Python 版 open(..., encoding='utf-8') 行为一致。
[System.IO.File]::WriteAllText($p, $content, (New-Object System.Text.UTF8Encoding($false)))
$result = @{ ok = $true; path = $p }
