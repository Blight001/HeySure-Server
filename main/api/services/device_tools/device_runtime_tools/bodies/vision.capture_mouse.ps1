# device runtime tool: vision.capture_mouse
# 约定：对象 $toolArgs 已注入；把结果赋给 $result。由 powershell-runner 在设备上执行（Windows PowerShell 5.1 兼容）。
# 此文件是该工具的默认实现（真相源）；AI 可经 mcp.manage_dynamic_tool 改写实例。
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -Namespace HS -Name Dpi -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();'
[void][HS.Dpi]::SetProcessDPIAware()
$r = 200
if ($toolArgs.radius) { $r = [int]$toolArgs.radius }
$pos = [System.Windows.Forms.Cursor]::Position
$left = [Math]::Max(0, $pos.X - $r)
$top = [Math]::Max(0, $pos.Y - $r)
$w = $r * 2
$h = $r * 2
$bmp = New-Object System.Drawing.Bitmap($w, $h)
$gfx = [System.Drawing.Graphics]::FromImage($bmp)
$gfx.CopyFromScreen($left, $top, 0, 0, $bmp.Size)
$gfx.Dispose()
$codec = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() | Where-Object { $_.MimeType -eq 'image/jpeg' }
$ep = New-Object System.Drawing.Imaging.EncoderParameters(1)
$ep.Param[0] = New-Object System.Drawing.Imaging.EncoderParameter([System.Drawing.Imaging.Encoder]::Quality, [long]70)
$ms = New-Object System.IO.MemoryStream
$bmp.Save($ms, $codec, $ep)
$b64 = [Convert]::ToBase64String($ms.ToArray())
$sendToUser = $true
if ($null -ne $toolArgs.send_to_user) { $sendToUser = [bool]$toolArgs.send_to_user }
$result = @{ dataUrl = 'data:image/jpeg;base64,' + $b64; width = $bmp.Width; height = $bmp.Height; send_to_user = $sendToUser }
$bmp.Dispose()
$ms.Dispose()
