#requires -Version 5.1
<#
.SYNOPSIS
  Установка WinDictate в %LOCALAPPDATA%\WinDictate.

.DESCRIPTION
  Копирует приложение из исходников в %LOCALAPPDATA%\WinDictate\app, создаёт отдельное
  Python-окружение (uv venv + faster-whisper/sounddevice/numpy/pystray/pillow), прогоняет
  самопроверку, включает автозапуск с Windows (HKCU Run) и запускает приложение.

.PARAMETER SourceRoot
  Каталог исходников (по умолчанию — родитель каталога scripts).

.PARAMETER NoAutostart
  Не включать автозапуск с Windows.

.PARAMETER NoStart
  Не запускать приложение после установки.

.PARAMETER SkipVenv
  Не создавать/обновлять venv (использовать уже существующий).

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
#>
[CmdletBinding()]
param(
    [string]$SourceRoot,
    [switch]$NoAutostart,
    [switch]$NoStart,
    [switch]$SkipVenv
)

$ErrorActionPreference = 'Stop'

# $PSScriptRoot is not available inside the param() default in Windows PowerShell,
# so resolve the project root here.
if (-not $SourceRoot) {
    $SourceRoot = Split-Path -Parent $PSScriptRoot
}

function Write-Step([string]$Text) { Write-Host "== $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text)   { Write-Host "   $Text" -ForegroundColor Green }
function Write-Warn([string]$Text) { Write-Host "   $Text" -ForegroundColor Yellow }

$dest     = Join-Path $env:LOCALAPPDATA 'WinDictate'
$appDir   = Join-Path $dest 'app'
$venvDir  = Join-Path $dest '.venv'
$python   = Join-Path $venvDir 'Scripts\python.exe'
$pythonw  = Join-Path $venvDir 'Scripts\pythonw.exe'
$runKey   = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$runName  = 'WinDictate'
$appPy    = Join-Path $appDir 'app.py'

Write-Step "Источник: $SourceRoot"
if (-not (Test-Path (Join-Path $SourceRoot 'app.py'))) {
    throw "app.py не найден в $SourceRoot. Запустите установщик из каталога win-dictate."
}

Write-Step 'Останавливаю запущенный экземпляр'
$stopped = 0
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -match 'WinDictate[\\/]app[\\/](app\.py|server[\\/]stt_server\.py)' } |
    ForEach-Object {
        try {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
            $stopped++
            Write-Ok "остановлен PID $($_.ProcessId)"
        } catch {
            Write-Warn "не удалось остановить PID $($_.ProcessId): $($_.Exception.Message)"
        }
    }
if ($stopped -eq 0) { Write-Ok 'запущенных экземпляров нет' }

Write-Step 'Копирую файлы приложения'
foreach ($dir in @($appDir, (Join-Path $appDir 'dictate'), (Join-Path $appDir 'server'), (Join-Path $dest 'logs'))) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}
Copy-Item (Join-Path $SourceRoot 'app.py') $appDir -Force
Copy-Item (Join-Path $SourceRoot 'dictate\*.py') (Join-Path $appDir 'dictate') -Force
Copy-Item (Join-Path $SourceRoot 'server\stt_server.py') (Join-Path $appDir 'server') -Force
if (Test-Path (Join-Path $SourceRoot 'assets')) {
    $assetsTarget = Join-Path $appDir 'assets'
    if (Test-Path $assetsTarget) { Remove-Item -Path $assetsTarget -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $assetsTarget | Out-Null
    Copy-Item (Join-Path $SourceRoot 'assets\*') $assetsTarget -Recurse -Force
}
Write-Ok $appDir

if (-not $SkipVenv) {
    Write-Step 'Проверяю Python-окружение'
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uv) {
        throw 'Не найден uv. Установите его (https://docs.astral.sh/uv/) или запустите с -SkipVenv, если venv уже есть.'
    }
    if (-not (Test-Path $python)) {
        Write-Step 'Создаю venv (Python 3.11)'
        & uv venv --python 3.11 $venvDir
        if ($LASTEXITCODE -ne 0) { throw 'uv venv завершился с ошибкой.' }
    }
    Write-Step 'Ставлю зависимости (faster-whisper, sounddevice, numpy, pystray, pillow)'
    $packages = @('faster-whisper==1.2.1', 'sounddevice', 'numpy', 'pystray', 'pillow')
    $hasNvidia = [bool](Get-Command nvidia-smi -ErrorAction SilentlyContinue)
    if ($hasNvidia) {
        # ctranslate2 needs the CUDA 12 cuBLAS + cuDNN 9 DLLs; stt_server.py adds them to
        # the DLL search path. Without them the sidecar silently degrades to CPU.
        $packages += 'nvidia-cublas-cu12', 'nvidia-cudnn-cu12>=9,<10'
        Write-Ok 'NVIDIA GPU найден — добавляю CUDA 12 runtime'
    }
    & uv pip install --python $python @packages
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить зависимости.' }
    Write-Ok 'зависимости готовы'
}

if (-not (Test-Path $pythonw)) { throw "Не найден $pythonw. Запустите без -SkipVenv." }

Write-Step 'Проверка шрифтов и иконок'
$assetProbe = @'
import sys
sys.path.insert(0, r"{app}")
from dictate import theme
import tkinter as tk
root = tk.Tk()
root.withdraw()
fonts = theme.register_fonts()
icon_photo = theme.icon_photo(root, "scene", 26) is not None
icon_files = theme.ICON_PATH.is_file() and theme.TRAY_ICON_PATH.is_file()
root.destroy()
print(f"fonts={fonts} icon_photo={icon_photo} icon_files={icon_files}")
sys.exit(0 if (fonts and icon_photo and icon_files) else 1)
'@.Replace('{app}', $appDir)
$assetFile = Join-Path $env:TEMP 'windictate-asset-probe.py'
$assetProbe | Set-Content -Path $assetFile -Encoding UTF8
$assetOutput = & $python $assetFile 2>&1
$assetCode = $LASTEXITCODE
$assetOutput | Out-Host
if ($assetCode -ne 0) { throw 'Не загружаются шрифты/иконки из установленной копии.' }
Write-Ok 'шрифты Nunito/Golos Text/JetBrains Mono и иконки на месте'

Write-Step 'Собираю WinDictate.exe (нативный лаунчер)'
$buildScript = Join-Path $SourceRoot 'scripts\build-launcher.ps1'
if (-not (Test-Path $buildScript)) { throw "Не найден $buildScript" }
$launcherProject = Join-Path $SourceRoot 'dist\WinDictate.exe'
& $buildScript -OutputPath $launcherProject | Out-Null
if (-not (Test-Path $launcherProject)) { throw 'Сборка WinDictate.exe не удалась.' }
$launcher = Join-Path $dest 'WinDictate.exe'
Copy-Item $launcherProject $launcher -Force
Write-Ok $launcher

Write-Step 'Создаю ярлыки (Рабочий стол и меню «Пуск»)'
$shortcutName = 'Диктовка.lnk'
$shortcutTargets = @(
    (Join-Path ([Environment]::GetFolderPath('Desktop')) $shortcutName),
    (Join-Path $env:APPDATA ("Microsoft\Windows\Start Menu\Programs\" + $shortcutName))
)
$shell = New-Object -ComObject WScript.Shell
foreach ($shortcutPath in $shortcutTargets) {
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $launcher
    $shortcut.WorkingDirectory = $dest
    $shortcut.IconLocation = "$launcher,0"
    $shortcut.Description = 'Локальная диктовка по горячей клавише'
    $shortcut.Save()
    Write-Ok $shortcutPath
}
# Миграция: ярлыки прежнего имени больше не нужны.
foreach ($old in @('СТОРИАРТ Диктовка.lnk')) {
    foreach ($folder in @(([Environment]::GetFolderPath('Desktop')), (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'))) {
        $oldPath = Join-Path $folder $old
        if (Test-Path $oldPath) { Remove-Item $oldPath -Force }
    }
}

Write-Step 'Копирую скрипты обслуживания'
Copy-Item (Join-Path $SourceRoot 'scripts\uninstall.ps1') $dest -Force
Copy-Item (Join-Path $SourceRoot 'scripts\run.ps1') $dest -Force
Write-Ok "$dest\uninstall.ps1, run.ps1"

Write-Step 'Самопроверка (app.py --selftest)'
$selfTestOutput = & $python $appPy --selftest 2>&1
$selfTestCode = $LASTEXITCODE
$selfTestOutput | Out-Host
if ($selfTestCode -ne 0) {
    Write-Warn "selftest вернул код $selfTestCode — смотрите вывод выше и $dest\logs\app.log"
} else {
    Write-Ok 'selftest пройден'
}

if (-not $NoAutostart) {
    Write-Step 'Включаю автозапуск с Windows'
    $command = '"' + $launcher + '" --autostart'
    New-Item -Path $runKey -Force | Out-Null
    Set-ItemProperty -Path $runKey -Name $runName -Value $command
    $stored = (Get-ItemProperty -Path $runKey -Name $runName).$runName
    if ($stored -ne $command) { throw 'Не удалось записать автозапуск в реестр.' }
    Write-Ok $command
}

Write-Step 'Регистрирую в «Программах и компонентах»'
$uninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\WinDictate'
New-Item -Path $uninstallKey -Force | Out-Null
Set-ItemProperty -Path $uninstallKey -Name 'DisplayName' -Value 'Диктовка (WinDictate)'
Set-ItemProperty -Path $uninstallKey -Name 'DisplayVersion' -Value '0.2.0'
Set-ItemProperty -Path $uninstallKey -Name 'Publisher' -Value 'WinDictate'
Set-ItemProperty -Path $uninstallKey -Name 'DisplayIcon' -Value $launcher
Set-ItemProperty -Path $uninstallKey -Name 'InstallLocation' -Value $dest
Set-ItemProperty -Path $uninstallKey -Name 'UninstallString' -Value ('powershell -NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $dest 'uninstall.ps1') + '"')
Set-ItemProperty -Path $uninstallKey -Name 'NoModify' -Value 1 -Type DWord
Set-ItemProperty -Path $uninstallKey -Name 'NoRepair' -Value 1 -Type DWord
Write-Ok $uninstallKey

if (-not $NoStart) {
    Write-Step 'Запускаю WinDictate'
    Start-Process -FilePath $launcher -ArgumentList '--start' -WorkingDirectory $dest
    Start-Sleep -Seconds 5
    $proc = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'WinDictate[\\/]app[\\/]app\.py' }
    if ($proc) {
        Write-Ok "работает, PID $($proc.ProcessId)"
    } else {
        Write-Warn "процесс не найден — смотрите $dest\logs\app.log и launcher.log"
    }
}

Write-Host ''
Write-Host 'Готово. WinDictate установлен.' -ForegroundColor Green
Write-Host "  Приложение:       $launcher"
Write-Host "  Ярлыки:           «Диктовка» на Рабочем столе и в меню «Пуск»"
Write-Host "  Горячая клавиша:  Ctrl+Alt+Space (старт/стоп)"
Write-Host "  Настройки:        Ctrl+Alt+S или меню в трее"
Write-Host "  Каталог:          $dest"
Write-Host "  Логи:             $dest\logs\app.log, launcher.log"
Write-Host "  Автозапуск:       $(if ($NoAutostart) { 'выключен (-NoAutostart)' } else { 'включён' })"
Write-Host ''
Write-Host 'Перезапуск: двойной клик по ярлыку «Диктовка» (спросит) или WinDictate.exe --restart.'
Write-Host 'Если горячая клавиша занята другой программой — смените её в настройках.'
