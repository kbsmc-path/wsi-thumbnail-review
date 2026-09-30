@echo off
rem WSI Thumbnail Review - double-click to start, or drag a folder onto this file.
rem Uses .venv if present, otherwise the Python on PATH. Missing packages are reported with install steps.
cd /d "%~dp0"
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (
    where python >nul 2>nul
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    where py >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
    echo Python 3.8 or newer is required.
    echo Install it from https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^), then run this again.
    pause
    exit /b 1
)
%PY% wsi_review.py %*
if errorlevel 1 pause
