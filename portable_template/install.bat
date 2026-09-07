@echo off
chcp 65001 >nul
REM ========================================
REM PhotoArrange API Service Installer (Portable, hardened)
REM   - Installs binaries to %ProgramFiles%\PhotoArrangeAPI (admin-only writable)
REM   - Runs service under a dedicated low-privilege local account
REM ========================================

echo ========================================
echo PhotoArrange API Service Installer
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
set "SERVICE_DISPLAY=PhotoArrange API Service"
set "SERVICE_DESC=AI Photo Organizer - Remote Analysis Service"
set "SVC_ACCOUNT=svc_photoarrange"

REM Source (extracted portable folder) and target (Program Files) paths
set "SRC_DIR=%~dp0"
set "INSTALL_DIR=%ProgramFiles%\PhotoArrangeAPI"
set "NSSM_EXE=%INSTALL_DIR%\nssm.exe"
set "API_EXE=%INSTALL_DIR%\api_server.dist\PhotoArrangeAPI.exe"
set "WORK_DIR=%INSTALL_DIR%\api_server.dist"
set "LOG_DIR=%INSTALL_DIR%\api_server.dist\logs"

echo Service Name:  %SERVICE_NAME%
echo Service User:  %SVC_ACCOUNT%
echo Source Dir:    %SRC_DIR%
echo Install Dir:   %INSTALL_DIR%
echo Executable:    %API_EXE%
echo.

if not exist "%SRC_DIR%nssm.exe" (
    echo [ERROR] Source nssm.exe not found in %SRC_DIR%
    pause
    exit /b 1
)
if not exist "%SRC_DIR%api_server.dist\PhotoArrangeAPI.exe" (
    echo [ERROR] Source PhotoArrangeAPI.exe not found in %SRC_DIR%api_server.dist
    pause
    exit /b 1
)

REM ------------------------------------------------------------------
REM [Step 0] Prompt for service account password (never persisted)
REM   Interactive input only; do not log/echo the password.
REM ------------------------------------------------------------------
echo [Step 0] Configure service account password
echo   The service will run under a dedicated low-privilege account: %SVC_ACCOUNT%
echo   Choose a strong password (min 12 chars, mix upper/lower/digit/symbol).
echo   The password is NOT saved to disk.
echo.
set "SVC_PASS="
set /p "SVC_PASS=Enter password for %SVC_ACCOUNT%: "
if "%SVC_PASS%"=="" (
    echo [ERROR] Password cannot be empty.
    pause
    exit /b 1
)

REM ------------------------------------------------------------------
REM [Step 1] Stop and remove old service if present
REM ------------------------------------------------------------------
sc query %SERVICE_NAME% >nul 2>&1
if %errorLevel% equ 0 (
    echo [Step 1] Existing service found, stopping and removing...
    net stop %SERVICE_NAME% >nul 2>&1
    if exist "%NSSM_EXE%" (
        "%NSSM_EXE%" remove %SERVICE_NAME% confirm >nul 2>&1
    ) else if exist "%SRC_DIR%nssm.exe" (
        "%SRC_DIR%nssm.exe" remove %SERVICE_NAME% confirm >nul 2>&1
    )
    timeout /t 2 /nobreak >nul
)

REM ------------------------------------------------------------------
REM [Step 2] Create service account if missing
REM ------------------------------------------------------------------
echo [Step 2] Ensuring service account %SVC_ACCOUNT% exists...
net user %SVC_ACCOUNT% >nul 2>&1
if %errorLevel% neq 0 (
    net user %SVC_ACCOUNT% "%SVC_PASS%" /add /passwordchg:no /y >nul
    if errorlevel 1 (
        echo [ERROR] Failed to create local account %SVC_ACCOUNT%.
        echo   Possible causes: weak password, policy restriction.
        pause
        exit /b 1
    )
    REM Password never expires + no interactive logon needed
    wmic useraccount where "Name='%SVC_ACCOUNT%'" set PasswordExpires=FALSE >nul 2>&1
    echo   Account %SVC_ACCOUNT% created.
) else (
    REM Update password to what was just entered (idempotent reinstall)
    net user %SVC_ACCOUNT% "%SVC_PASS%" >nul
    echo   Account %SVC_ACCOUNT% already exists (password updated).
)

REM Deny interactive/remote logon (best-effort; ignore failures)
REM ntrights.exe or secpol tweaks omitted here to avoid dependencies; NSSM will
REM grant SeServiceLogonRight automatically when ObjectName is set.

REM ------------------------------------------------------------------
REM [Step 3] Copy binaries to admin-only install directory
REM ------------------------------------------------------------------
echo [Step 3] Copying binaries to %INSTALL_DIR% ...
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
robocopy "%SRC_DIR%api_server.dist" "%INSTALL_DIR%\api_server.dist" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 (
    echo [ERROR] Failed to copy api_server.dist to install dir.
    pause
    exit /b 1
)
copy /Y "%SRC_DIR%nssm.exe" "%INSTALL_DIR%\nssm.exe" >nul
copy /Y "%SRC_DIR%uninstall.bat" "%INSTALL_DIR%\uninstall.bat" >nul 2>&1

REM ------------------------------------------------------------------
REM [Step 4] Harden ACLs on install dir
REM   - Remove inherited permissions
REM   - Grant SYSTEM + Administrators full control
REM   - Grant service account read+execute only (no write to binaries)
REM ------------------------------------------------------------------
echo [Step 4] Hardening install directory ACL...
icacls "%INSTALL_DIR%" /inheritance:r >nul
icacls "%INSTALL_DIR%" /grant:r "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" "%SVC_ACCOUNT%:(OI)(CI)RX" >nul

REM Service must write logs -> loosen just the logs dir
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
icacls "%LOG_DIR%" /grant:r "%SVC_ACCOUNT%:(OI)(CI)M" >nul

REM ------------------------------------------------------------------
REM [Step 5] Grant service account write access to task data dir
REM   base_dir is read from api_config.yaml at runtime; users may customize it.
REM   Here we grant the default D:\AIPhotoArrange_api if it exists / can be created.
REM ------------------------------------------------------------------
echo [Step 5] Preparing task data directory ACL...
set "TASK_BASE_DIR=D:\AIPhotoArrange_api"
if not exist "%TASK_BASE_DIR%" (
    mkdir "%TASK_BASE_DIR%" 2>nul
)
if exist "%TASK_BASE_DIR%" (
    icacls "%TASK_BASE_DIR%" /grant:r "%SVC_ACCOUNT%:(OI)(CI)M" "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" >nul
    echo   Task data dir: %TASK_BASE_DIR%
) else (
    echo   [WARN] Could not create %TASK_BASE_DIR%; adjust api_config.yaml and ACL manually.
)

REM ------------------------------------------------------------------
REM [Step 6] Install service via NSSM under low-privilege account
REM ------------------------------------------------------------------
echo [Step 6] Installing service under %SVC_ACCOUNT% ...
"%NSSM_EXE%" install %SERVICE_NAME% "%API_EXE%"
if %errorLevel% neq 0 (
    echo [ERROR] Service install failed
    pause
    exit /b 1
)

"%NSSM_EXE%" set %SERVICE_NAME% AppDirectory "%WORK_DIR%"
"%NSSM_EXE%" set %SERVICE_NAME% DisplayName "%SERVICE_DISPLAY%"
"%NSSM_EXE%" set %SERVICE_NAME% Description "%SERVICE_DESC%"
"%NSSM_EXE%" set %SERVICE_NAME% Start SERVICE_AUTO_START
"%NSSM_EXE%" set %SERVICE_NAME% AppStdout "%LOG_DIR%\api_service_stdout.log"
"%NSSM_EXE%" set %SERVICE_NAME% AppStderr "%LOG_DIR%\api_service_stderr.log"
"%NSSM_EXE%" set %SERVICE_NAME% AppExit Default Restart
"%NSSM_EXE%" set %SERVICE_NAME% AppRestartDelay 5000
"%NSSM_EXE%" set %SERVICE_NAME% ObjectName ".\%SVC_ACCOUNT%" "%SVC_PASS%"
if errorlevel 1 (
    echo [ERROR] Failed to bind service to account %SVC_ACCOUNT%.
    echo   Check that the password meets policy and the account is not locked.
    pause
    exit /b 1
)

REM Clear password variable from memory
set "SVC_PASS="

echo [Step 7] Starting service...
net start %SERVICE_NAME%
if %errorLevel% neq 0 (
    echo [ERROR] Service start failed
    echo Log: %LOG_DIR%\api_service_stderr.log
    pause
    exit /b 1
)

echo.
echo ========================================
echo Service installed successfully!
echo ========================================
echo.
sc query %SERVICE_NAME%
echo.
echo Install path: %INSTALL_DIR%
echo Runs as:      .\%SVC_ACCOUNT%
echo.
echo Commands:
echo   Start:     net start %SERVICE_NAME%
echo   Stop:      net stop %SERVICE_NAME%
echo   Uninstall: %INSTALL_DIR%\uninstall.bat
echo.
echo Health check: http://YOUR_IP:36600/api/health
echo.
pause
