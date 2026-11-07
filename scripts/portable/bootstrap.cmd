@echo off
rem WinDictate portable - first run setup.
rem Creates runtime\ (Python 3.11 + faster-whisper + sounddevice + numpy + pystray + pillow)
rem next to this file. Requires uv on PATH (https://docs.astral.sh/uv/).
setlocal
cd /d "%~dp0"

echo.
echo   WinDictate portable - first run setup
echo   ====================================
echo.

where uv >nul 2>nul
if errorlevel 1 goto no_uv

if not exist runtime\Scripts\pythonw.exe (
    echo Creating Python 3.11 environment in runtime\ ...
    uv venv --python 3.11 runtime
    if errorlevel 1 goto fail
)

echo Installing dependencies ^(faster-whisper, sounddevice, numpy, pystray, pillow^) ...
uv pip install --python runtime\Scripts\python.exe faster-whisper==1.2.1 sounddevice numpy pystray pillow
if errorlevel 1 goto fail

where nvidia-smi >nul 2>nul
if not errorlevel 1 (
    echo NVIDIA GPU found - installing CUDA 12 runtime ...
    uv pip install --python runtime\Scripts\python.exe nvidia-cublas-cu12 "nvidia-cudnn-cu12>=9,<10"
    if errorlevel 1 goto fail
)

if not exist data mkdir data
if not exist data\logs mkdir data\logs

echo.
echo Done. You can start WinDictate.exe now.
echo.
if /i "%~1"=="--quiet" exit /b 0
pause
exit /b 0

:no_uv
echo ERROR: uv was not found on PATH.
echo Install uv from https://docs.astral.sh/uv/ and run this file again.
if /i "%~1"=="--quiet" exit /b 1
pause
exit /b 1

:fail
echo ERROR: setup failed, see the messages above.
if /i "%~1"=="--quiet" exit /b 1
pause
exit /b 1
