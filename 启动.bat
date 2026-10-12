@echo off
rem =====================================================================
rem  Focus Work Log  --  double-click to start
rem  NOTE: this file is intentionally ASCII-only.  cmd.exe mis-parses
rem  UTF-8/Chinese text under the default code page, so all Chinese
rem  messages and all real logic live in  launch.pyw  instead.
rem  The Python file it starts is also ASCII-named for the same reason.
rem =====================================================================
cd /d "%~dp0"
title Focus Work Log

set "LAUNCH="

if exist "%LOCALAPPDATA%\Programs\Python\Launcher\pyw.exe" set "LAUNCH=%LOCALAPPDATA%\Programs\Python\Launcher\pyw.exe"
if not defined LAUNCH if exist "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" set "LAUNCH=%LOCALAPPDATA%\Programs\Python\Launcher\py.exe"
if not defined LAUNCH where pyw.exe >nul 2>&1 && set "LAUNCH=pyw.exe"
if not defined LAUNCH where pythonw.exe >nul 2>&1 && set "LAUNCH=pythonw.exe"
if not defined LAUNCH where py.exe >nul 2>&1 && set "LAUNCH=py.exe"
if not defined LAUNCH where python.exe >nul 2>&1 && set "LAUNCH=python.exe"

if not defined LAUNCH goto NOPY
if not exist "%~dp0launch.pyw" goto NOSCRIPT

start "" %LAUNCH% "%~dp0launch.pyw"
goto OK

:NOSCRIPT
echo.
echo   [ERROR] launch.pyw was not found in this folder.
echo   Please make sure the whole folder was copied, not just this file.
echo.
pause
goto END

:NOPY
echo.
echo   [ERROR] Python was not found on this computer.
echo   Please install Python 3 from https://www.python.org/downloads/
echo   and tick "Add python.exe to PATH" during setup, then run this file again.
echo.
pause
goto END

:OK
echo.
echo   Starting... the browser will open automatically in a few seconds.
echo   If it does not open, see  ????.txt  in this folder, or visit
echo   http://127.0.0.1:8090/  in your browser manually.
echo.
timeout /t 3 /nobreak >nul

:END
endlocal
exit /b 0