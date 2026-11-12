@echo off
setlocal
cd /d "%~dp0"

set "SCRIPT=twitch_live_organizer.py"

if not exist "%SCRIPT%" (
    echo [error] %SCRIPT% not found in %CD%.
    goto :error
)

if not exist "requirements.txt" (
    echo [error] requirements.txt not found in %CD%.
    goto :error
)

where py >nul 2>&1
if not errorlevel 1 (
    py -3 --version >nul 2>&1
    if not errorlevel 1 goto :python_ready
)

echo [..] Python launcher not found or not usable, installing Python...
where winget >nul 2>&1
if errorlevel 1 (
    echo [error] winget is not available. Install Python manually from:
    echo         https://www.python.org/downloads/ and enable the py launcher.
    goto :error
)

winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
if errorlevel 1 (
    echo [error] Python installation failed.
    goto :error
)

if defined LOCALAPPDATA if exist "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" (
    set "PATH=%LOCALAPPDATA%\Programs\Python\Launcher;%PATH%"
)

py -3 --version >nul 2>&1
if errorlevel 1 (
    echo [error] Python was installed but py is not available in this terminal.
    echo         Close this window, open a new one, and run install.bat again.
    goto :error
)

:python_ready
for /f "delims=" %%V in ('py -3 --version 2^>^&1') do echo [ok] %%V

echo [..] Installing tray dependencies...
py -3 -m pip install --user -r requirements.txt
if errorlevel 1 (
    echo [error] Could not install tray dependencies.
    goto :error
)

echo [..] Configuring Twitch API credentials...
py -3 "%SCRIPT%" --configure-credentials
if errorlevel 1 (
    echo [error] Could not configure Twitch API credentials.
    goto :error
)

echo [..] Installing startup launcher...
py -3 "%SCRIPT%" --install-startup
if errorlevel 1 (
    echo [error] Could not install the startup launcher.
    goto :error
)

echo [ok] Installation complete.
echo [..] Starting Twitch Live Organizer...
where pyw >nul 2>&1
if errorlevel 1 (
    echo [error] Python windowless launcher pyw was not found.
    goto :error
)
start "" pyw -3 "%~dp0%SCRIPT%"
if errorlevel 1 (
    echo [error] Could not start Twitch Live Organizer.
    goto :error
)

echo.
timeout /t 2 /nobreak >nul
exit /b 0

:error
echo.
pause
exit /b 1
