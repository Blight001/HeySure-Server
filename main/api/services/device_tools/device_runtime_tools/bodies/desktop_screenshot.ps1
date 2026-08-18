# device runtime tool: desktop_screenshot
# $toolArgs is injected; assign output to $result. Windows PowerShell 5.1.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -Namespace HS -Name Dpi -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();'
[void][HS.Dpi]::SetProcessDPIAware()

function Encode-Jpeg($bmp, [long]$quality) {
    $codec = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() | Where-Object { $_.MimeType -eq 'image/jpeg' }
    $ep = New-Object System.Drawing.Imaging.EncoderParameters(1)
    $ep.Param[0] = New-Object System.Drawing.Imaging.EncoderParameter([System.Drawing.Imaging.Encoder]::Quality, $quality)
    $ms = New-Object System.IO.MemoryStream
    $bmp.Save($ms, $codec, $ep)
    $b64 = [Convert]::ToBase64String($ms.ToArray())
    $ms.Dispose()
    return $b64
}

function Shrink-IfWide($bmp, [int]$maxWidth) {
    if ($bmp.Width -le $maxWidth) { return $bmp }
    $nw = $maxWidth
    $nh = [Math]::Max(1, [int][Math]::Floor($bmp.Height * $maxWidth / $bmp.Width))
    $small = New-Object System.Drawing.Bitmap($bmp, $nw, $nh)
    $bmp.Dispose()
    return $small
}

$action = ([string]$toolArgs.action).ToLower()
if (-not $action) { $action = 'full' }

if ($action -eq 'region') {
    if (($null -eq $toolArgs.width) -or ($null -eq $toolArgs.height)) { throw 'width and height are required' }
    $x = [int]$toolArgs.x; $y = [int]$toolArgs.y
    $w = [int]$toolArgs.width; $h = [int]$toolArgs.height
    $bmp = New-Object System.Drawing.Bitmap($w, $h)
    $gfx = [System.Drawing.Graphics]::FromImage($bmp)
    $gfx.CopyFromScreen($x, $y, 0, 0, $bmp.Size)
    $gfx.Dispose()
    $quality = 70
} elseif ($action -eq 'around_mouse') {
    $r = 200
    if ($toolArgs.radius) { $r = [int]$toolArgs.radius }
    $pos = [System.Windows.Forms.Cursor]::Position
    $left = [Math]::Max(0, $pos.X - $r)
    $top = [Math]::Max(0, $pos.Y - $r)
    $bmp = New-Object System.Drawing.Bitmap($r * 2, $r * 2)
    $gfx = [System.Drawing.Graphics]::FromImage($bmp)
    $gfx.CopyFromScreen($left, $top, 0, 0, $bmp.Size)
    $gfx.Dispose()
    $quality = 70
} elseif ($action -eq 'full') {
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
    $bmp = Shrink-IfWide $bmp 1280
    $quality = 60
} else {
    throw "unsupported action: $action"
}

$b64 = Encode-Jpeg $bmp $quality
$sendToUser = $true
if ($null -ne $toolArgs.send_to_user) { $sendToUser = [bool]$toolArgs.send_to_user }
$result = @{
    action = $action
    dataUrl = 'data:image/jpeg;base64,' + $b64
    width = $bmp.Width
    height = $bmp.Height
    send_to_user = $sendToUser
}
$bmp.Dispose()
