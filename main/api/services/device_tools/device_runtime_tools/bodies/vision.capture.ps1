# device runtime tool: vision.capture
# 约定：对象 $toolArgs 已注入；把结果赋给 $result。由 powershell-runner 在设备上执行（Windows PowerShell 5.1 兼容）。
# 此文件是该工具的默认实现（真相源）；AI 可经 mcp.manage_dynamic_tool 改写实例。
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -Namespace HS -Name Dpi -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();'
[void][HS.Dpi]::SetProcessDPIAware()
$disp = 0
if ($toolArgs.display) { $disp = [int]$toolArgs.display }
$screens = [System.Windows.Forms.Screen]::AllScreens
if (($disp -ge 0) -and ($disp -lt $screens.Count)) {
    $bounds = $screens[$disp].Bounds
} else {
    $bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
}
$bmp = New-Object System.Drawing.Bitmap($bounds.Width, $bounds.Height)
$gfx = [System.Drawing.Graphics]::FromImage($bmp)
$gfx.CopyFromScreen($bounds.Left, $bounds.Top, 0, 0, $bmp.Size)
$gfx.Dispose()
if ($bmp.Width -gt 1280) {
    $nw = 1280
    $nh = [Math]::Max(1, [int][Math]::Floor($bmp.Height * 1280 / $bmp.Width))
    $small = New-Object System.Drawing.Bitmap($bmp, $nw, $nh)
    $bmp.Dispose()
    $bmp = $small
}
$codec = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() | Where-Object { $_.MimeType -eq 'image/jpeg' }
$ep = New-Object System.Drawing.Imaging.EncoderParameters(1)
$ep.Param[0] = New-Object System.Drawing.Imaging.EncoderParameter([System.Drawing.Imaging.Encoder]::Quality, [long]60)
$ms = New-Object System.IO.MemoryStream
$bmp.Save($ms, $codec, $ep)
$b64 = [Convert]::ToBase64String($ms.ToArray())
$sendToUser = $true
if ($null -ne $toolArgs.send_to_user) { $sendToUser = [bool]$toolArgs.send_to_user }
$result = @{ dataUrl = 'data:image/jpeg;base64,' + $b64; width = $bmp.Width; height = $bmp.Height; send_to_user = $sendToUser }
$bmp.Dispose()
$ms.Dispose()
