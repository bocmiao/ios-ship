@echo off
setlocal enabledelayedexpansion

REM ---------------------------------------------------------------
REM  ios-ship launcher
REM
REM  Pure ASCII on purpose. cmd.exe reads .cmd files in the OEM code
REM  page (GBK on Chinese Windows), so UTF-8 Chinese text inside a
REM  batch file gets chopped into fragments and executed as commands.
REM  All Chinese output comes from Python, never from this file.
REM ---------------------------------------------------------------

set "HERE=%~dp0"
set "PY="
set "PYARGS="

REM  Switch the console to UTF-8 so Python's Chinese output renders.
REM  Without this the console decodes UTF-8 bytes as GBK and every
REM  Chinese character turns into garbage.
chcp 65001 >nul 2>nul

REM  0) Explicit override: set PY=C:\path\to\python.exe before running.
if defined PY_OVERRIDE goto :havepython

REM  1) python on PATH.
REM     `where python` alone is not enough: Windows ships a 0-byte App
REM     Execution Alias at AppData\Local\Microsoft\WindowsApps\python.exe
REM     that opens the Store instead of running. So actually execute it.
python -c "import sys" >nul 2>nul
if !errorlevel! equ 0 (
  set "PY=python"
  goto :havepython
)

REM  2) The py launcher installed by python.org.
py -3 -c "import sys" >nul 2>nul
if !errorlevel! equ 0 (
  set "PY=py"
  set "PYARGS=-3"
  goto :havepython
)

REM  3) Per-user install, which is the default for python.org on Windows.
for %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
  if exist "%%~D\python.exe" set "PY=%%~D\python.exe"
)

:havepython
if defined PY_OVERRIDE set "PY=%PY_OVERRIDE%"

if not defined PY goto :nopython

"%PY%" %PYARGS% "%HERE%ship.py" %*
exit /b %errorlevel%

:nopython
echo.
echo  [ERROR] No usable Python found.
echo.
echo  Install Python 3.9+ from https://www.python.org/downloads/
echo  and tick "Add python.exe to PATH" during setup.
echo.
echo  Already installed? Point this launcher at it:
echo    set PY_OVERRIDE=C:\path\to\python.exe
echo    ship.cmd doctor
echo.
echo  Note: python.exe under WindowsApps is a Store stub, not a real
echo  interpreter -- it does nothing when run from a batch file.
echo.
exit /b 1
