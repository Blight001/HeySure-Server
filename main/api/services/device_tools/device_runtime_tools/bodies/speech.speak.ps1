# device runtime tool: speech.speak
# 约定：对象 $toolArgs 已注入；把结果赋给 $result。由 powershell-runner 在设备上执行（Windows PowerShell 5.1 兼容）。
# 此文件是该工具的默认实现（真相源）；AI 可经 device_mcp.manage（action=upsert 同名工具）改写实例。
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    if ($null -ne $toolArgs.rate) {
        # 入参沿用 pyttsx3 的每分钟词数（默认约 200）；SpeechSynthesizer.Rate 取值 -10..10。
        $wpm = [int]$toolArgs.rate
        $synth.Rate = [Math]::Max(-10, [Math]::Min(10, [int][Math]::Round(($wpm - 200) / 20)))
    }
    if ($null -ne $toolArgs.volume) {
        # 0-100，与 Python 版（volume/100 → pyttsx3 0..1）对外语义一致。
        $synth.Volume = [Math]::Max(0, [Math]::Min(100, [int]$toolArgs.volume))
    }
    $synth.Speak([string]$toolArgs.text)
} finally {
    $synth.Dispose()
}
$result = @{ ok = $true }
