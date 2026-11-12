@echo off
setlocal
cd /d "%~dp0"

set "ORGANIZER_SCRIPT=%~dp0twitch_live_organizer.py"
set "STARTUP_LAUNCHER=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\TwitchLiveOrganizer.cmd"

if not defined APPDATA (
    echo [error] APPDATA is not set; cannot locate the Startup folder.
    goto :error
)

if exist "%STARTUP_LAUNCHER%" (
    del /f /q "%STARTUP_LAUNCHER%"
    if errorlevel 1 (
        echo [error] Could not remove the startup launcher.
        goto :error
    )
    echo [ok] Removed startup launcher.
) else (
    echo [ok] Startup launcher was not installed.
)

echo [..] Stopping Twitch Live Organizer if it is running...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference = 'Stop'; try { $scriptPath = [IO.Path]::GetFullPath($env:ORGANIZER_SCRIPT); $processes = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('python.exe', 'pythonw.exe') -and $_.CommandLine -and $_.CommandLine.IndexOf($scriptPath, [StringComparison]::OrdinalIgnoreCase) -ge 0 }); if ($processes.Count -eq 0) { Write-Output 'No running organizer process found.' } else { foreach ($process in $processes) { Stop-Process -Id $process.ProcessId -Force; Write-Output ('Stopped process ' + $process.ProcessId) } } } catch { Write-Error $_; exit 1 }"
if errorlevel 1 (
    echo [error] Could not stop the organizer process.
    goto :error
)

echo [ok] Uninstallation complete.
echo.
timeout /t 2 /nobreak >nul
exit /b 0

:error
echo.
pause
exit /b 1
