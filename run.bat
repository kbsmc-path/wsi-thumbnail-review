@echo off
rem WSI Thumbnail Review - double-click to start, or drag a folder onto this file.
rem Uses the first Python 3.8+ that actually runs: .venv\Scripts\python.exe, then python, then the py launcher.
rem If Pillow is missing it is installed once with "python -m pip" (not pip.exe - Smart App Control blocks
rem unsigned launchers). If Windows blocks this file ("Smart App Control blocked" / "Dangerous file extension
rem from the web"), right-click the downloaded ZIP > Properties > Unblock and extract again - see README.
setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
if exist "%PY%" call :usable && goto found
set "PY=python"
call :usable && goto found
set "PY=py -3"
call :usable && goto found

echo Python 3.8 or newer is required.
echo Install it from https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^), then run this again.
pause
exit /b 1

:usable
rem Succeeds only if %PY% really starts and is 3.8+ (the Microsoft Store "python" stub fails here).
call %PY% -c "import sys; sys.exit(sys.version_info < (3, 8))" >nul 2>nul
exit /b

:found
call %PY% -c "import PIL.Image" >nul 2>nul
if not errorlevel 1 goto run
echo Pillow is not installed - installing it now (first run only) ...
call %PY% -m pip install -r requirements.txt
if not errorlevel 1 goto run
echo.
echo Pillow could not be installed. Check the internet connection or ask IT, then run in this folder:
echo     %PY% -m pip install -r requirements.txt
pause
exit /b 1

:run
%PY% wsi_review.py %*
if errorlevel 1 pause
