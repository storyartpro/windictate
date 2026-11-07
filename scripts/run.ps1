#requires -Version 5.1
<#
.SYNOPSIS
  Запуск установленного WinDictate.

.PARAMETER Settings
  Открыть сразу окно настроек.

.PARAMETER Restart
  Перезапустить приложение.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run.ps1
#>
[CmdletBinding()]
param(
    [switch]$Settings,
    [switch]$Restart
)

$ErrorActionPreference = 'Stop'
$dest     = Join-Path $env:LOCALAPPDATA 'WinDictate'
$launcher = Join-Path $dest 'WinDictate.exe'
$pythonw  = Join-Path $dest '.venv\Scripts\pythonw.exe'
$appPy    = Join-Path $dest 'app\app.py'

if (Test-Path $launcher) {
    $arguments = @('--start')
    if ($Restart) { $arguments = @('--restart') }
    elseif ($Settings) { $arguments = @('--settings') }
    Start-Process -FilePath $launcher -ArgumentList $arguments -WorkingDirectory $dest
    Start-Sleep -Seconds 1
    Write-Host 'WinDictate запущен (WinDictate.exe). Старт/стоп диктовки — Ctrl+Alt+Space.' -ForegroundColor Green
    return
}

# Fallback for a source install without the launcher.
if (-not (Test-Path $appPy) -or -not (Test-Path $pythonw)) {
    throw "WinDictate не установлен ($dest). Сначала запустите scripts\install.ps1."
}
$running = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -match 'WinDictate[\\/]app[\\/]app\.py' }
if ($running) {
    Write-Host "WinDictate уже работает (PID $($running.ProcessId))." -ForegroundColor Yellow
    return
}
$arguments = @('"' + $appPy + '"')
if ($Settings) { $arguments += '--settings' } else { $arguments += '--autostart' }
Start-Process -FilePath $pythonw -ArgumentList $arguments -WorkingDirectory (Join-Path $dest 'app')
Start-Sleep -Seconds 2
Write-Host 'WinDictate запущен. Старт/стоп диктовки — Ctrl+Alt+Space.' -ForegroundColor Green
