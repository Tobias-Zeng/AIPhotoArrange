@echo off
chcp 65001 >nul
REM ========================================
REM PhotoArrange API Service Uninstaller (Portable)
REM   - Stops and removes the Windows service
REM   - Does NOT delete the service account or task data (manual cleanup)
REM ========================================

echo ========================================
echo PhotoArrange API Service Uninstaller
echo ========================================
echo.

net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [ERROR] Administrator privileges required.
    echo Right-click this script and select "Run as administrator".
    pause
    exit /b 1
)

set "SERVICE_NAME=PhotoArrangeAPI"
set "INSTALL_DIR=%ProgramFiles%\PhotoArrangeAPI"
set "NSSM_EXE=%INSTALL_DIR%\nssm.exe"

if not exist "%NSSM_EXE%" (
    REM Fallback to script directory (running from portable folder before install)
    set "NSSM_EXE=%~dp0nssm.exe"
)

sc query %SERVICE_NAME% >nul 2>&1
if %errorLevel% neq 0 (
    echo [INFO] Service does not exist, nothing to uninstall.
    pause
    exit /b 0
)

echo Stopping service...
net stop %SERVICE_NAME% >nul 2>&1

echo Removing service...
"%NSSM_EXE%" remove %SERVICE_NAME% confirm

echo.
echo Service uninstalled.
echo.
echo Notes:
echo   - Local account 'svc_photoarrange' is NOT removed (delete manually if unused).
echo   - Install dir kept at: %INSTALL_DIR%
echo   - Task data dir kept at: D:\AIPhotoArrange_api
echo.
pause
