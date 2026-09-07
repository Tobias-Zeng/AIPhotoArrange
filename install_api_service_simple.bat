@echo off
chcp 65001 >nul
REM ========================================
REM PhotoArrange API Service - dev/local installer (hardened)
REM   - Installs binaries to %ProgramFiles%\PhotoArrangeAPI (admin-only writable)
REM   - Runs service under a dedicated low-privilege local account
REM ========================================

echo ========================================
echo PhotoArrange API Service (dev installer)
echo ========================================
echo.

net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [ERROR] Administrator privileges required.
    pause
    exit /b 1
)

set "SERVICE_NAME=PhotoArrangeAPI"
set "SERVICE_DISPLAY=PhotoArrange API Service"
set "SERVICE_DESC=AI Photo Organizer - Remote Analysis Service"
set "SVC_ACCOUNT=svc_photoarrange"

set "SRC_DIR=%~dp0release\api_server.dist\"
set "SRC_NSSM=%~dp0nssm.exe"
set "INSTALL_DIR=%ProgramFiles%\PhotoArrangeAPI"
set "NSSM_EXE=%INSTALL_DIR%\nssm.exe"
set "API_EXE=%INSTALL_DIR%\api_server.dist\PhotoArrangeAPI.exe"
set "WORK_DIR=%INSTALL_DIR%\api_server.dist"
set "LOG_DIR=%INSTALL_DIR%\api_server.dist\logs"

if not exist "%SRC_NSSM%" (
    echo [ERROR] Source nssm.exe not found: %SRC_NSSM%
    pause
    exit /b 1
)
if not exist "%SRC_DIR%PhotoArrangeAPI.exe" (
    echo [ERROR] Source PhotoArrangeAPI.exe not found. Run: python build_api_exe.py
    pause
    exit /b 1
)

echo Service Name:  %SERVICE_NAME%
echo Service User:  %SVC_ACCOUNT%
echo Source Dir:    %SRC_DIR%
echo Install Dir:   %INSTALL_DIR%
echo Executable:    %API_EXE%
echo.

set "SVC_PASS="
set /p "SVC_PASS=Enter password for %SVC_ACCOUNT%: "
if "%SVC_PASS%"=="" (
    echo [ERROR] Password cannot be empty.
    pause
    exit /b 1
)

sc query %SERVICE_NAME% >nul 2>&1
if %errorLevel% equ 0 (
    echo [Step 1] Existing service found, stopping and removing...
    net stop %SERVICE_NAME% >nul 2>&1
    if exist "%NSSM_EXE%" (
        "%NSSM_EXE%" remove %SERVICE_NAME% confirm >nul 2>&1
    ) else (
        "%SRC_NSSM%" remove %SERVICE_NAME% confirm >nul 2>&1
    )
    timeout /t 2 /nobreak >nul
)

echo [Step 2] Ensuring service account %SVC_ACCOUNT% exists...
net user %SVC_ACCOUNT% >nul 2>&1
if %errorLevel% neq 0 (
    net user %SVC_ACCOUNT% "%SVC_PASS%" /add /passwordchg:no /y >nul
    if errorlevel 1 (
        echo [ERROR] Failed to create local account %SVC_ACCOUNT%.
        pause
        exit /b 1
    )
    wmic useraccount where "Name='%SVC_ACCOUNT%'" set PasswordExpires=FALSE >nul 2>&1
    echo   Account %SVC_ACCOUNT% created.
) else (
    net user %SVC_ACCOUNT% "%SVC_PASS%" >nul
    echo   Account %SVC_ACCOUNT% already exists (password updated).
)

echo [Step 3] Copying binaries to %INSTALL_DIR% ...
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
robocopy "%SRC_DIR%" "%INSTALL_DIR%\api_server.dist" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 (
    echo [ERROR] Failed to copy api_server.dist to install dir.
    pause
    exit /b 1
)
copy /Y "%SRC_NSSM%" "%INSTALL_DIR%\nssm.exe" >nul

echo [Step 4] Hardening install directory ACL...
icacls "%INSTALL_DIR%" /inheritance:r >nul
icacls "%INSTALL_DIR%" /grant:r "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" "%SVC_ACCOUNT%:(OI)(CI)RX" >nul
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
icacls "%LOG_DIR%" /grant:r "%SVC_ACCOUNT%:(OI)(CI)M" >nul

echo [Step 5] Preparing task data directory ACL...
set "TASK_BASE_DIR=D:\AIPhotoArrange_api"
if not exist "%TASK_BASE_DIR%" mkdir "%TASK_BASE_DIR%" 2>nul
if exist "%TASK_BASE_DIR%" (
    icacls "%TASK_BASE_DIR%" /grant:r "%SVC_ACCOUNT%:(OI)(CI)M" "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" >nul
    echo   Task data dir: %TASK_BASE_DIR%
) else (
    echo   [WARN] Could not create %TASK_BASE_DIR%; adjust api_config.yaml and ACL manually.
)

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
    pause
    exit /b 1
)

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
echo Health check: http://YOUR_IP:36600/api/health
echo.
pause
