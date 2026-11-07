#requires -Version 5.1
<#
.SYNOPSIS
    Собирает портативную версию WinDictate в dist\WinDictate-portable.zip.

.DESCRIPTION
    Кладёт в архив управляющий WinDictate.exe, код приложения (app\), bootstrap.cmd
    для создания окружения при первом запуске, portable.flag и README. Ничего не
    устанавливает в систему: распаковал — запустил.

.PARAMETER OutputPath
    Куда положить zip. По умолчанию dist\WinDictate-portable.zip.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-portable.ps1
#>
[CmdletBinding()]
param(
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $OutputPath) {
    $OutputPath = Join-Path $projectRoot 'dist\WinDictate-portable.zip'
}

$appSource = Join-Path $projectRoot 'app.py'
if (-not (Test-Path $appSource)) {
    throw "app.py не найден в $projectRoot"
}

$staging = Join-Path $projectRoot 'dist\_portable'
if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }

$appDir = Join-Path $staging 'app'
foreach ($dir in @($staging, $appDir, (Join-Path $appDir 'dictate'), (Join-Path $appDir 'server'))) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

Write-Host '== Копирую файлы приложения' -ForegroundColor Cyan
Copy-Item $appSource $appDir -Force
Copy-Item (Join-Path $projectRoot 'dictate\*.py') (Join-Path $appDir 'dictate') -Force
Copy-Item (Join-Path $projectRoot 'server\stt_server.py') (Join-Path $appDir 'server') -Force
if (Test-Path (Join-Path $projectRoot 'assets')) {
    Copy-Item (Join-Path $projectRoot 'assets') $appDir -Recurse -Force
}
Write-Host '   app\ готов' -ForegroundColor Green

Write-Host '== Собираю WinDictate.exe' -ForegroundColor Cyan
$exe = Join-Path $staging 'WinDictate.exe'
& (Join-Path $projectRoot 'scripts\build-launcher.ps1') -OutputPath $exe | Out-Null
if (-not (Test-Path $exe)) { throw 'WinDictate.exe не собран.' }

Copy-Item (Join-Path $projectRoot 'scripts\portable\bootstrap.cmd') $staging -Force
Copy-Item (Join-Path $projectRoot 'scripts\portable\README.txt') $staging -Force
Set-Content -Path (Join-Path $staging 'portable.flag') -Value 'WinDictate portable' -Encoding ASCII
New-Item -ItemType Directory -Force -Path (Join-Path $staging 'data\logs') | Out-Null
Set-Content -Path (Join-Path $staging 'data\logs\.keep') -Value '' -Encoding ASCII
Write-Host '   bootstrap.cmd, README.txt, portable.flag, data\ готовы' -ForegroundColor Green

Write-Host '== Упаковываю zip' -ForegroundColor Cyan
$outDir = Split-Path -Parent $OutputPath
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
if (Test-Path $OutputPath) { Remove-Item $OutputPath -Force }
Compress-Archive -Path (Join-Path $staging '*') -DestinationPath $OutputPath -CompressionLevel Optimal
if (-not (Test-Path $OutputPath)) { throw 'Не удалось создать zip.' }
Remove-Item $staging -Recurse -Force

$file = Get-Item $OutputPath
$hash = (Get-FileHash $OutputPath -Algorithm SHA256).Hash
Write-Host ''
Write-Host '== Готово' -ForegroundColor Cyan
Write-Host ("   {0} ({1:N1} МБ)" -f $file.FullName, ($file.Length / 1MB)) -ForegroundColor Green
Write-Host "   SHA256: $hash"
Write-Output $OutputPath
