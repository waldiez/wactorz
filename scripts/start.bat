@echo off
:: SPDX-License-Identifier: Apache-2.0
:: Wactorz - Windows launcher (double-click wrapper).
::
:: Runs start.ps1 from the same folder, with any arguments passed on, and exits
:: with its exit code. Everything the launcher does is in start.ps1.
::
:: The window stays open after an error so it can be read; set
:: WACTORZ_NO_PAUSE=1 to skip that when calling this from another script.
::
:: No labels or goto: the repository stores this file with LF line endings,
:: and cmd can miss a label in a file without CRLF.

setlocal
set "LAUNCHER=%~dp0start.ps1"
set "CODE=0"

if exist "%LAUNCHER%" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%LAUNCHER%" %*
) else (
    echo.
    echo ERROR: start.ps1 was not found next to start.bat in %~dp0
    echo Run start.bat from the scripts folder of a Wactorz checkout, where the
    echo two files sit together. To get a checkout:
    echo     git clone https://github.com/waldiez/wactorz
    echo.
    cmd /c exit 1
)
set "CODE=%ERRORLEVEL%"

if not "%CODE%"=="0" if not "%WACTORZ_NO_PAUSE%"=="1" pause
exit /b %CODE%
