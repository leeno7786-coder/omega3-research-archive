@echo off
REM ------------------------------------------------------------------
REM hud-only.bat - portable HUD entrypoint owned by Omega.exe.
REM The public Omega.exe is resolved from the SSD layout before any
REM hidden runtime is touched.  No machine Python installation is used.
REM
REM Usage:
REM   scripts\hud-only.bat                -> http on port 13310 (default)
REM   scripts\hud-only.bat 13311          -> alternate port
REM   scripts\hud-only.bat 13310 --https  -> https with local CA cert
REM   scripts\hud-only.bat 13310 --no-boot -> empty placeholders only
REM ------------------------------------------------------------------
setlocal
set "PRODUCT_ROOT=%~dp0.."
for %%I in ("%PRODUCT_ROOT%") do set "PRODUCT_ROOT=%%~fI"
set "OMEGA_EXE=%PRODUCT_ROOT%\Omega.exe"
if not exist "%OMEGA_EXE%" set "OMEGA_EXE=%PRODUCT_ROOT%\..\Omega.exe"
if not exist "%OMEGA_EXE%" (
    echo [hud-only] FATAL: Omega.exe was not found beside the portable runtime.
    echo [hud-only] Checked:
    echo [hud-only]   "%PRODUCT_ROOT%\Omega.exe"
    echo [hud-only]   "%PRODUCT_ROOT%\..\Omega.exe"
    exit /b 1
)

REM Ask the Omega-owned HUD path to prepare its portable Edge debugger profile
REM before opening the browser window.  --no-browser remains available as an
REM explicit diagnostic override in the extra arguments below.
set "OMEGA_HUD_OPEN_BROWSER=1"

set "PORT=%1"
if "%PORT%"=="" set "PORT=13310"

shift
set "EXTRA="
:gather
if "%~1"=="" goto run
set "EXTRA=%EXTRA% %1"
shift
goto gather

:run
cd /d "%PRODUCT_ROOT%"
echo [hud-only] routing through "%OMEGA_EXE%" on port %PORT% (extra: %EXTRA%)
if defined EXTRA (
    "%OMEGA_EXE%" --launch-hud --port %PORT%%EXTRA%
) else (
    "%OMEGA_EXE%" --launch-hud --port %PORT%
)
exit /b %ERRORLEVEL%
