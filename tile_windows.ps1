Add-Type -AssemblyName System.Windows.Forms

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public static class Win32 {
  [DllImport("user32.dll", SetLastError = true)]
  public static extern bool SetWindowPos(
    IntPtr hWnd,
    IntPtr hWndInsertAfter,
    int X,
    int Y,
    int cx,
    int cy,
    uint uFlags);

  [DllImport("user32.dll")]
  public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
}
"@

$createdNew = $false
$mutex = New-Object System.Threading.Mutex($false, 'Global\HeySureAI.RunLauncher', [ref]$createdNew)
if (-not $createdNew) {
  exit 0
}

try {
  $jobs = @(
    @{ Key = 'G'; Title = 'HeySure Gateway'; Script = 'run_gateway.bat' },
    @{ Key = 'M'; Title = 'HeySure MCP Runtime'; Script = 'run_mcp.bat' },
    @{ Key = 'C'; Title = 'HeySure Connector Runtime'; Script = 'run_connector.bat' },
    @{ Key = 'A'; Title = 'HeySure AI Runtime'; Script = 'run_ai.bat' }
  )

  $screen = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
  $halfW = [math]::Floor($screen.Width / 2)
  $halfH = [math]::Floor($screen.Height / 2)
  $positions = @(
    @{ X = $screen.Left;           Y = $screen.Top },
    @{ X = $screen.Left;           Y = $screen.Top + $halfH },
    @{ X = $screen.Left + $halfW;   Y = $screen.Top },
    @{ X = $screen.Left + $halfW;   Y = $screen.Top + $halfH }
  )

  $scriptDir = $PSScriptRoot
  $serviceProcesses = @{}

  function Get-ServiceProcess {
    param(
      [Parameter(Mandatory = $true)]
      [string]$Title
    )

    Get-Process cmd -ErrorAction SilentlyContinue |
      Where-Object { $_.MainWindowTitle -eq $Title } |
      Select-Object -First 1
  }

  function Set-ServiceProcess {
    param(
      [Parameter(Mandatory = $true)]
      [string]$Title,
      [Parameter(Mandatory = $false)]
      [System.Diagnostics.Process]$Process
    )

    if ($Process) {
      $serviceProcesses[$Title] = $Process
    } elseif ($serviceProcesses.ContainsKey($Title)) {
      $serviceProcesses.Remove($Title)
    }
  }

  function Resolve-ServiceProcess {
    param(
      [Parameter(Mandatory = $true)]
      [string]$Title
    )

    if ($serviceProcesses.ContainsKey($Title)) {
      $cached = $serviceProcesses[$Title]
      if ($cached -and -not $cached.HasExited) {
        return $cached
      }
      $serviceProcesses.Remove($Title)
    }

    $proc = Get-ServiceProcess -Title $Title
    if ($proc) {
      Set-ServiceProcess -Title $Title -Process $proc
    }

    return $proc
  }

  function Wait-WindowHandle {
    param(
      [Parameter(Mandatory = $true)]
      [System.Diagnostics.Process]$Process
    )

    for ($i = 0; $i -lt 40 -and $Process.MainWindowHandle -eq 0; $i++) {
      Start-Sleep -Milliseconds 250
      $Process.Refresh()
    }

    return $Process.MainWindowHandle
  }

  function Start-ServiceWindow {
    param(
      [Parameter(Mandatory = $true)]
      [string]$Title,
      [Parameter(Mandatory = $true)]
      [string]$Script
    )

    $proc = Start-Process -FilePath 'cmd.exe' -WorkingDirectory $scriptDir -PassThru -ArgumentList '/k', "title $Title && call $Script"
    Set-ServiceProcess -Title $Title -Process $proc
    return $proc
  }

  function Stop-ServiceWindow {
    param(
      [Parameter(Mandatory = $true)]
      [string]$Title
    )

    $proc = Resolve-ServiceProcess -Title $Title
    if ($proc) {
      & taskkill /F /T /PID $proc.Id | Out-Null
      for ($i = 0; $i -lt 40; $i++) {
        if (-not (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue)) {
          break
        }
        Start-Sleep -Milliseconds 200
      }
      Set-ServiceProcess -Title $Title -Process $null
    }
  }

  function Ensure-ServiceWindow {
    param(
      [Parameter(Mandatory = $true)]
      [string]$Title,
      [Parameter(Mandatory = $true)]
      [string]$Script
    )

    $proc = Resolve-ServiceProcess -Title $Title
    if (-not $proc) {
      $proc = Start-ServiceWindow -Title $Title -Script $Script
    }

    if (Wait-WindowHandle -Process $proc) {
      return $proc
    }

    return $proc
  }

  function Tile-ServiceWindows {
    param(
      [Parameter(Mandatory = $true)]
      [object[]]$Processes
    )

    for ($i = 0; $i -lt $jobs.Count; $i++) {
      $proc = $Processes[$i]
      if ($proc -and $proc.MainWindowHandle -ne 0) {
        [Win32]::ShowWindow($proc.MainWindowHandle, 9) | Out-Null
        [Win32]::SetWindowPos(
          $proc.MainWindowHandle,
          [IntPtr]::Zero,
          $positions[$i].X,
          $positions[$i].Y,
          $halfW,
          $halfH,
          0x0054
        ) | Out-Null
      }
    }
  }

  function Refresh-AllWindows {
    $processes = New-Object object[] $jobs.Count
    for ($i = 0; $i -lt $jobs.Count; $i++) {
      $processes[$i] = Ensure-ServiceWindow -Title $jobs[$i].Title -Script $jobs[$i].Script
    }

    Tile-ServiceWindows -Processes $processes
  }

  function Restart-ServiceWindow {
    param(
      [Parameter(Mandatory = $true)]
      [string]$Title,
      [Parameter(Mandatory = $true)]
      [string]$Script
    )

    Write-Host "Restarting $Title..."
    Stop-ServiceWindow -Title $Title
    Start-Sleep -Milliseconds 300
    Refresh-AllWindows
    Write-Host "$Title restarted."
  }

  function Stop-AllServices {
    foreach ($job in $jobs) {
      Stop-ServiceWindow -Title $job.Title
    }
  }

  function Restart-AllServices {
    Write-Host "Restarting all services..."
    Stop-AllServices
    Start-Sleep -Milliseconds 300
    Refresh-AllWindows
    Write-Host "All services restarted."
  }

  Write-Host "HeySure control console"
  Write-Host "S = exit all"
  Write-Host "R = restart all"
  Write-Host "M = restart MCP"
  Write-Host "A = restart AI"
  Write-Host "G = restart Gateway"
  Write-Host "C = restart Connector"
  Write-Host ""

  Refresh-AllWindows

  $exitRequested = $false
  while (-not $exitRequested) {
    $keyInfo = [Console]::ReadKey($true)
    $key = [string]$keyInfo.KeyChar

    switch ($key.ToUpperInvariant()) {
      'S' {
        Write-Host "Stopping all services..."
        Stop-AllServices
        $exitRequested = $true
      }
      'R' { Restart-AllServices }
      'M' { Restart-ServiceWindow -Title 'HeySure MCP Runtime' -Script 'run_mcp.bat' }
      'A' { Restart-ServiceWindow -Title 'HeySure AI Runtime' -Script 'run_ai.bat' }
      'G' { Restart-ServiceWindow -Title 'HeySure Gateway' -Script 'run_gateway.bat' }
      'C' { Restart-ServiceWindow -Title 'HeySure Connector Runtime' -Script 'run_connector.bat' }
      default { }
    }
  }
}
finally {
  try { $mutex.ReleaseMutex() | Out-Null } catch { }
  $mutex.Dispose()
}
