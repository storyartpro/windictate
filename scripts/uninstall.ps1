#requires -Version 5.1
<#
.SYNOPSIS
  Удаление WinDictate.

.DESCRIPTION
  Останавливает приложение, убирает автозапуск, ярлыки и запись в «Программах и компонентах»,
  удаляет %LOCALAPPDATA%\WinDictate. С -KeepData сохраняет настройки, историю и логи.

.PARAMETER KeepData
  Сохранить settings.json, history.json и logs.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\uninstall.ps1 -KeepData
#>
[CmdletBinding()]
param([switch]$KeepData)

$ErrorActionPreference = 'Stop'
$dest     = Join-Path $env:LOCALAPPDATA 'WinDictate'
$launcher = Join-Path $dest 'WinDictate.exe'
$runKey   = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$uninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\WinDictate'
$runName  = 'WinDictate'

Write-Host 'Останавливаю WinDictate...' -ForegroundColor Cyan
if (Test-Path $launcher) {
    try {
        Start-Process -FilePath $launcher -ArgumentList '--stop' -WorkingDirectory $dest -Wait
        Write-Host '  приложение остановлено через WinDictate.exe'
    } catch {
        Write-Host "  не удалось вызвать WinDictate.exe --stop: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -match 'WinDictate[\\/]app[\\/](app\.py|server[\\/]stt_server\.py)' } |
    ForEach-Object {
        try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; Write-Host "  остановлен PID $($_.ProcessId)" }
        catch { Write-Host "  не удалось остановить PID $($_.ProcessId)" -ForegroundColor Yellow }
    }

Write-Host 'Убираю автозапуск...' -ForegroundColor Cyan
$existing = Get-ItemProperty -Path $runKey -Name $runName -ErrorAction SilentlyContinue
if ($existing) {
    Remove-ItemProperty -Path $runKey -Name $runName -ErrorAction SilentlyContinue
    Write-Host '  значение WinDictate удалено'
} else {
    Write-Host '  автозапуска не было'
}

Write-Host 'Убираю запись из «Программ и компонентов»...' -ForegroundColor Cyan
if (Test-Path $uninstallKey) {
    Remove-Item -Path $uninstallKey -Recurse -Force
    Write-Host '  запись удалена'
}

Write-Host 'Убираю ярлыки...' -ForegroundColor Cyan
$shortcutNames = @('Диктовка.lnk', 'СТОРИАРТ Диктовка.lnk')
$shortcutFolders = @(
    ([Environment]::GetFolderPath('Desktop')),
    (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs')
)
foreach ($folder in $shortcutFolders) {
    foreach ($name in $shortcutNames) {
        $path = Join-Path $folder $name
        if (Test-Path $path) {
            Remove-Item -Path $path -Force
            Write-Host "  удалён $path"
        }
    }
}

if (-not (Test-Path $dest)) {
    Write-Host 'Каталог приложения не найден — удалять нечего.' -ForegroundColor Yellow
    return
}

if ($KeepData) {
    Write-Host 'Удаляю приложение и .venv (данные сохраняются)...' -ForegroundColor Cyan
    foreach ($sub in @('app', '.venv', 'WinDictate.exe', 'uninstall.ps1', 'run.ps1', 'README.txt')) {
        $path = Join-Path $dest $sub
        if (Test-Path $path) { Remove-Item -Path $path -Recurse -Force }
    }
    Write-Host "Данные остались в $dest" -ForegroundColor Green
} else {
    Write-Host "Удаляю $dest ..." -ForegroundColor Cyan
    Remove-Item -Path $dest -Recurse -Force
    Write-Host 'WinDictate удалён.' -ForegroundColor Green
}
