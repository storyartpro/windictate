#requires -Version 5.1
<#
.SYNOPSIS
    ообирает WinDictate.exe — нативный лаунчер приложения (оТОРИАРТ Диктовка).

.DESCRIPTION
    Компилирует scripts\launcher\WinDictateLauncher.cs компилятором C# из .NET Framework
    (csc.exe, входит в Windows) в dist\WinDictate.exe с фирменной иконкой и версией.
    Никаких внешних зависимостей и установки SDK не нужно.

.PARAMETER OutputPath
    Куда положить exe. По умолчанию dist\WinDictate.exe.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-launcher.ps1
#>
[CmdletBinding()]
param(
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$source      = Join-Path $PSScriptRoot 'launcher\WinDictateLauncher.cs'
$icon        = Join-Path $projectRoot 'assets\icons\windictate.ico'
if (-not $OutputPath) {
    $OutputPath = Join-Path $projectRoot 'dist\WinDictate.exe'
}

if (-not (Test-Path $source)) { throw "Не найден исходник лаунчера: $source" }

$candidates = @(
    (Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'),
    (Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe')
)
$csc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $csc) {
    throw 'Не найден csc.exe из .NET Framework 4.x (Windows PowerShell 5.1 обычно уже содержит его).'
}

$outDir = Split-Path -Parent $OutputPath
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
if (Test-Path $OutputPath) { Remove-Item $OutputPath -Force }

$references = @(
    'System.dll',
    'System.Core.dll',
    'System.Drawing.dll',
    'System.Windows.Forms.dll',
    'System.Management.dll'
)

$arguments = @(
    '/nologo',
    '/target:winexe',
    '/optimize+',
    '/platform:anycpu',
    "/out:$OutputPath"
)
if (Test-Path $icon) { $arguments += "/win32icon:$icon" }
foreach ($reference in $references) { $arguments += "/r:$reference" }
$arguments += $source

Write-Host "== Компилирую лаунчер" -ForegroundColor Cyan
Write-Host "   csc: $csc"
Write-Host "   out: $OutputPath"
& $csc @arguments
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $OutputPath)) {
    throw "Компиляция не удалась (exit $LASTEXITCODE)."
}

$file = Get-Item $OutputPath
Write-Host ("   готово: {0} ({1:N0} КБ)" -f $file.FullName, ($file.Length / 1KB)) -ForegroundColor Green
Write-Output $OutputPath
