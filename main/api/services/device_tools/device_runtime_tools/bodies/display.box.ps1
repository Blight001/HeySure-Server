# device runtime tool: display.box
# 约定：对象 $toolArgs 已注入；把结果赋给 $result。由 powershell-runner 在设备上执行（Windows PowerShell 5.1 兼容）。
# 此文件是该工具的默认实现（真相源）；AI 可经 mcp.manage_dynamic_tool 改写实例。
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -Namespace HS -Name Dpi -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();'
[void][HS.Dpi]::SetProcessDPIAware()
if (($null -eq $toolArgs.width) -or ($null -eq $toolArgs.height)) { throw 'width and height are required' }
$left = [int]$toolArgs.left
$top = [int]$toolArgs.top
$width = [int]$toolArgs.width
$height = [int]$toolArgs.height
$duration = 1000
if ($toolArgs.duration) { $duration = [int]$toolArgs.duration }
$color = [string]$toolArgs.color
if (-not $color) { $color = 'red' }
$label = [string]$toolArgs.label

$form = New-Object System.Windows.Forms.Form
$form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$form.StartPosition = [System.Windows.Forms.FormStartPosition]::Manual
$form.Location = New-Object System.Drawing.Point($left, $top)
$form.Size = New-Object System.Drawing.Size($width, $height)
$form.TopMost = $true
$form.ShowInTaskbar = $false
try { $form.BackColor = [System.Drawing.ColorTranslator]::FromHtml($color) } catch { $form.BackColor = [System.Drawing.Color]::Red }
$form.Opacity = 0.35
if ($label) {
    $lbl = New-Object System.Windows.Forms.Label
    $lbl.Text = $label
    $lbl.AutoSize = $true
    $lbl.ForeColor = [System.Drawing.Color]::White
    $lbl.BackColor = $form.BackColor
    $lbl.Location = New-Object System.Drawing.Point(2, 2)
    $form.Controls.Add($lbl)
}
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = [Math]::Max(1, $duration)
$timer.Add_Tick({ $form.Close() })
$timer.Start()
[System.Windows.Forms.Application]::Run($form)
$timer.Dispose()
$result = @{ ok = $true }
