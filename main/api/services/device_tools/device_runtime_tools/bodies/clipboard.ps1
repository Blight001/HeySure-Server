# device runtime tool: clipboard
# $toolArgs is injected; assign output to $result. Windows PowerShell 5.1.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms

$action = ([string]$toolArgs.action).ToLower()
if ($action -eq 'set') {
    $text = [string]$toolArgs.text
    if ([string]::IsNullOrEmpty($text)) {
        [System.Windows.Forms.Clipboard]::Clear()
    } else {
        [System.Windows.Forms.Clipboard]::SetText($text)
    }
    $result = @{ ok = $true; action = 'set' }
} elseif ($action -eq 'get' -or -not $action) {
    $text = ''
    if ([System.Windows.Forms.Clipboard]::ContainsText()) {
        $text = [System.Windows.Forms.Clipboard]::GetText()
    }
    $result = @{ action = 'get'; text = $text }
} else {
    throw "unsupported action: $action"
}
