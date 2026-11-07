@echo off
setlocal
cd /d "%~dp0"
set "tagcleaner_python=.venv-audio\Scripts\python.exe"
set "tagcleaner_uv=%~dp0runtime\uv.exe"
set "PATH=%~dp0runtime\audio;%PATH%"
if not exist "%tagcleaner_uv%" set "tagcleaner_uv=uv"
if /i "%~1"=="--setup-only" goto setup
if not exist "%tagcleaner_python%" goto setup
if not exist "runtime\setup-v1.done" goto setup
goto launch

:setup
echo Preparing TagCleaner. The first launch downloads Python, packages and audio tools.
set "UV_PYTHON_INSTALL_DIR=%~dp0runtime\python"
set "UV_CACHE_DIR=%~dp0runtime\uv-cache"
set "UV_PYTHON_INSTALL_REGISTRY=0"
if not exist "runtime" mkdir "runtime"
if not exist "%tagcleaner_python%" (
    "%tagcleaner_uv%" venv --managed-python --python 3.12 .venv-audio
    if errorlevel 1 goto failed
)
"%tagcleaner_uv%" pip install --python "%tagcleaner_python%" -r requirements-windows.txt
if errorlevel 1 goto failed
"%tagcleaner_python%" tools\setup_audio_runtime.py
if errorlevel 1 (
    if /i "%~1"=="--setup-only" goto failed
    echo Audio recognition tools are incomplete. Tag and cover editing are available.
    echo Run TagCleaner.cmd --setup-only later to retry the audio tools.
)
type nul > "runtime\setup-v1.done"
if errorlevel 1 goto failed
if /i "%~1"=="--setup-only" exit /b 0

:launch
if /i "%~1"=="--browser" (
    "%tagcleaner_python%" gui.py
) else (
    "%tagcleaner_python%" desktop.py
)
if errorlevel 1 goto failed
exit /b 0

:failed
echo.
echo TagCleaner setup or launch failed. See the error above and README.md.
if /i not "%~1"=="--setup-only" pause
exit /b 1
