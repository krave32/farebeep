@echo off
rem =====================================================================
rem  FareBeep - dev launcher (Windows)
rem  Opens TWO windows:
rem    1. "FareBeep Server"  - the FastAPI webhook server  (port 8000)
rem    2. "FareBeep Tunnel"  - public HTTPS URL for Meta webhook
rem  Copy the https://...trycloudflare.com URL from window 2 into the
rem  Meta webhook config (and re-run if the URL ever changes).
rem =====================================================================
cd /d "%~dp0.."

start "FareBeep Server" cmd /k "python -m uvicorn FareBeep.main:app --port 8000"

if "%1"=="" (
    set CFTUNNEL=%LOCALAPPDATA%\Programs\cloudflared\cloudflared.exe
) else (
    set CFTUNNEL=%1
)

echo  FareBeep Server starting...
timeout /t 3 >nul
start "FareBeep Tunnel" cmd /k ""%CFTUNNEL%" tunnel --url http://localhost:8000"

echo.
echo  Server  : http://localhost:8000
echo  Health  : http://localhost:8000/health
echo  Tunnel  : copy the https://*.trycloudflare.com URL from the Tunnel window
echo.
pause