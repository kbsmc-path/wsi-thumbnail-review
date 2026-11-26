@echo off
rem WSI Thumbnail Review - double-click to start, or drag a folder onto this file.
rem
rem Needs Python 3.8+ (with tkinter) and Pillow. Python does NOT have to be on PATH: this file looks in
rem   1. .venv next to this file      2. the "py" launcher      3. python / python3 on PATH
rem   4. the usual install folders (python.org per-user / all-users, C:\Python3x, Anaconda / Miniconda)
rem and uses the first Python 3.8+ that actually runs.
rem If Pillow is missing it is installed once with "python -m pip" (not pip.exe - Smart App Control blocks
rem unsigned launchers). With no Python at all it offers exe\WSI_Review_v*.exe if you built one.
rem If Windows blocks this file ("Smart App Control blocked" / "Dangerous file extension from the web"),
rem right-click the downloaded ZIP - Properties - Unblock and extract again - see README.
rem
rem Troubleshooting:  set WSI_REVIEW_CHECK=1  before running prints the Python that would be used and stops.
setlocal EnableExtensions
cd /d "%~dp0"
set "PY="

if exist ".venv\Scripts\python.exe" call :try ".venv\Scripts\python.exe"
call :try py -3
call :try python
call :try python3
for /d %%D in ("%LocalAppData%\Programs\Python\Python3*" "%ProgramFiles%\Python3*" "%SystemDrive%\Python3*") do call :try "%%~D\python.exe"
for %%D in ("%UserProfile%\anaconda3" "%UserProfile%\miniconda3" "%LocalAppData%\anaconda3" "%LocalAppData%\miniconda3" "%ProgramData%\anaconda3" "%ProgramData%\miniconda3") do call :try "%%~D\python.exe"
if not defined PY goto :no_python

if defined WSI_REVIEW_CHECK (
    echo Python: %PY%
    %PY% -c "import sys; print(sys.version)"
)

%PY% -c "import tkinter" >nul 2>nul
if errorlevel 1 goto :no_tkinter
%PY% -c "import PIL.ImageTk" >nul 2>nul
if errorlevel 1 goto :no_pillow

:start
if defined WSI_REVIEW_CHECK (
    echo tkinter and Pillow: OK
    exit /b 0
)
%PY% wsi_review.py %*
if errorlevel 1 pause
exit /b

rem ---- %* = a Python command; keep the first one that really runs and is 3.8 or newer ----
rem (running it, not "where": the Microsoft Store placeholder python.exe exists but is not Python)
:try
if defined PY exit /b
if "%~2"=="" if not "%~x1"=="" if not exist "%~1" exit /b
%* -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul
if not errorlevel 1 set PY=%*
exit /b

:no_pillow
echo  Pillow is not installed - installing it now (first run only) ...
%PY% -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :pillow_failed
%PY% -c "import PIL.ImageTk" >nul 2>nul
if not errorlevel 1 goto :start

:pillow_failed
echo.
echo  Pillow could not be installed. Check the internet connection or ask IT, then run in this folder:
echo      %PY% -m pip install -r requirements.txt
goto :offer_exe

:no_tkinter
echo.
echo  This Python has no tkinter (the GUI library):
echo      %PY%
echo  Reinstall Python from https://www.python.org/downloads/ and keep "tcl/tk and IDLE" ticked.
goto :offer_exe

:no_python
echo.
echo  Python 3.8 or newer was not found on this PC.
echo  (looked for: .venv here, the py launcher, python on PATH, the usual install folders)
echo.
echo  Install it from https://www.python.org/downloads/  - tick "Add python.exe to PATH" -
echo  or run:   winget install Python.Python.3.12
goto :offer_exe

:offer_exe
set "EXE="
for %%E in ("exe\WSI_Review_v*.exe" "WSI_Review_v*.exe") do set "EXE=%%~fE"
if not defined EXE goto :no_exe
echo.
echo  A stand-alone build that needs no Python is here:
echo      %EXE%
echo.
choice /c YN /m "  Start it now"
if errorlevel 2 exit /b 1
start "" "%EXE%" %*
exit /b 0

:no_exe
echo.
pause
exit /b 1
