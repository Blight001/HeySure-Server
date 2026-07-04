# device runtime tool: git.diff
# 约定：对象 $toolArgs 已注入；把结果赋给 $result。由 powershell-runner 在设备上执行（Windows PowerShell 5.1 兼容）。
# 此文件是该工具的默认实现（真相源）；AI 可经 mcp.manage_dynamic_tool 改写实例。
$ErrorActionPreference = 'Stop'
$rel = [string]$toolArgs.cwd
if (-not $rel) { $rel = '.' }
if ([System.IO.Path]::IsPathRooted($rel)) { $cwd = $rel } else { $cwd = Join-Path (Get-Location).Path $rel }
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = 'git'
$psi.Arguments = '-C "' + ($cwd -replace '"', '\"') + '" diff'
$psi.UseShellExecute = $false
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
$psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
$proc = [System.Diagnostics.Process]::Start($psi)
$errTask = $proc.StandardError.ReadToEndAsync()
$stdout = $proc.StandardOutput.ReadToEnd()
$proc.WaitForExit()
$stderr = $errTask.Result
if ($stdout.Length -gt 100000) { $stdout = $stdout.Substring(0, 100000) }
if ($stderr.Length -gt 4000) { $stderr = $stderr.Substring(0, 4000) }
$result = @{ stdout = $stdout; stderr = $stderr; exitCode = $proc.ExitCode }
