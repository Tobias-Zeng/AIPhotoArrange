<#
.SYNOPSIS
    编译 PhotoCleaner Release APK（A+C 方案：轮询检查 + 超时兜底）。

.DESCRIPTION
    解决 agent 编译 APK 时无法判断“卡死”还是“正常无输出”的问题：
      A. 后台启动 gradle，主循环每 5s 轮询打印心跳（证明进程存活）；
      B. 构建前删除旧 APK，确保检测到的是本次产物；
      C. 设置硬超时（默认 300s），到点仍无产物则判定失败并杀进程。

    编译成功后自动把 app-release.apk 复制为带版本号的文件名
    （从 app/build.gradle.kts 的 versionName 解析）。

.PARAMETER TimeoutSec
    最大等待秒数，默认 300（5 分钟）。实际编译约 1-2 分钟。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File build_apk.ps1
#>
param(
    [int]$TimeoutSec = 300
)

$ErrorActionPreference = "Stop"

# 脚本所在目录即 PhotoCleaner 项目根
$ProjectDir = $PSScriptRoot
Set-Location $ProjectDir

# ---- 环境 ----
$JavaHome = "C:\Program Files\Microsoft\jdk-17.0.20.8-hotspot"
$GradleBat = Join-Path $env:USERPROFILE ".gradle\wrapper\dists\gradle-8.2-bin\gradle-8.2\bin\gradle.bat"

if (-not (Test-Path $GradleBat)) {
    Write-Host "[错误] 找不到 gradle.bat: $GradleBat" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $JavaHome)) {
    Write-Host "[错误] 找不到 JDK: $JavaHome" -ForegroundColor Red
    exit 1
}

$env:JAVA_HOME = $JavaHome

$ApkPath = Join-Path $ProjectDir "app\build\outputs\apk\release\app-release.apk"
$LogOut  = Join-Path $ProjectDir "build_apk.out.log"
$LogErr  = Join-Path $ProjectDir "build_apk.err.log"

# ---- B. 构建前清理旧产物 ----
if (Test-Path $ApkPath) {
    Remove-Item $ApkPath -Force
    Write-Host "[清理] 已删除旧 APK" -ForegroundColor DarkGray
}
foreach ($f in @($LogOut, $LogErr)) {
    if (Test-Path $f) { Remove-Item $f -Force }
}

# ---- 后台启动 gradle ----
Write-Host "[启动] gradle assembleRelease (超时 ${TimeoutSec}s)..." -ForegroundColor Cyan
$proc = Start-Process -FilePath $GradleBat `
    -ArgumentList "assembleRelease", "--console=plain" `
    -WorkingDirectory $ProjectDir `
    -NoNewWindow -PassThru `
    -RedirectStandardOutput $LogOut `
    -RedirectStandardError $LogErr

# ---- A. 轮询心跳 + C. 超时兜底 ----
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$pollSec = 5
while ($true) {
    Start-Sleep -Seconds $pollSec
    $elapsed = [int]$sw.Elapsed.TotalSeconds

    if ($proc.HasExited) {
        Write-Host "[结束] gradle 进程退出，code=$($proc.ExitCode) (耗时 ${elapsed}s)"
        break
    }

    # 打印日志尾部最后一行作为进度心跳
    $tail = ""
    if (Test-Path $LogOut) {
        $tail = (Get-Content $LogOut -Tail 1 -ErrorAction SilentlyContinue)
    }
    Write-Host "[等待 ${elapsed}s] 编译进行中... $tail"

    if ($elapsed -ge $TimeoutSec) {
        # C. 超时：先看产物是否已出现（进程可能刚好完成）
        if (Test-Path $ApkPath) {
            Write-Host "[超时但已产出] APK 已存在，判定成功" -ForegroundColor Yellow
            break
        }
        Write-Host "[超时] ${TimeoutSec}s 内未产出 APK，判定卡死，杀进程" -ForegroundColor Red
        try { $proc.Kill() } catch {}
        Write-Host "---- 日志尾部 ----"
        if (Test-Path $LogOut) { Get-Content $LogOut -Tail 30 }
        if (Test-Path $LogErr) { Get-Content $LogErr -Tail 30 }
        exit 1
    }
}

# ---- 结果判定 ----
if (-not (Test-Path $ApkPath)) {
    Write-Host "[失败] 编译进程已退出但未找到 APK" -ForegroundColor Red

    # 条件重试：仅当错误为 R8/文件占用（gradle daemon 残留锁文件）时，clean 后重试一次。
    # Kotlin 编译错误等代码问题不重试，立即失败暴露。
    $errContent = ""
    if (Test-Path $LogErr) { $errContent += (Get-Content $LogErr -Raw -ErrorAction SilentlyContinue) }
    if (Test-Path $LogOut) { $errContent += (Get-Content $LogOut -Raw -ErrorAction SilentlyContinue) }

    $isFileLock = $errContent -match "FileSystemException|classes\.dex|另一个程序正在使用|正在使用此文件|being used by another process"

    if ($isFileLock -and -not $env:APK_BUILD_RETRY) {
        Write-Host "[检测到文件占用错误] gradle clean 后重试一次..." -ForegroundColor Yellow
        & $GradleBat "clean" --console=plain 2>&1 | Out-Null
        Start-Sleep -Seconds 2
        $env:APK_BUILD_RETRY = "1"
        & powershell -ExecutionPolicy Bypass -File $PSCommandPath -TimeoutSec $TimeoutSec
        $code = $LASTEXITCODE
        Remove-Item Env:\APK_BUILD_RETRY -ErrorAction SilentlyContinue
        exit $code
    }

    if ($isFileLock -and $env:APK_BUILD_RETRY) {
        Write-Host "[重试仍失败] clean 后再次编译依旧未产出 APK" -ForegroundColor Red
    }

    Write-Host "---- stdout 尾部 ----"
    if (Test-Path $LogOut) { Get-Content $LogOut -Tail 30 }
    Write-Host "---- stderr 尾部 ----"
    if (Test-Path $LogErr) { Get-Content $LogErr -Tail 30 }
    exit 1
}

if (-not $proc.HasExited) {
    # 产物已出现但进程还没完全退出（收尾），给它一点时间
    $proc.WaitForExit(15000) | Out-Null
}
if ($proc.HasExited -and $proc.ExitCode -ne 0) {
    Write-Host "[警告] gradle 退出码非 0 ($($proc.ExitCode))，但 APK 已生成" -ForegroundColor Yellow
}

# ---- 解析版本号并复制为带版本名的文件 ----
$versionName = $null
$gradleFile = Join-Path $ProjectDir "app\build.gradle.kts"
if (Test-Path $gradleFile) {
    $pattern = 'versionName = "([^"]+)"'
    $m = Select-String -Path $gradleFile -Pattern $pattern | Select-Object -First 1
    if ($m) { $versionName = $m.Matches[0].Groups[1].Value }
}

$apkInfo = Get-Item $ApkPath
$sizeMB = [math]::Round($apkInfo.Length / 1MB, 2)
Write-Host "[成功] APK 生成: $($apkInfo.FullName) (${sizeMB} MB)" -ForegroundColor Green

if ($versionName) {
    $named = Join-Path $ProjectDir "PhotoCleaner-v$versionName-release.apk"
    Copy-Item $ApkPath $named -Force
    Write-Host "[复制] -> PhotoCleaner-v$versionName-release.apk" -ForegroundColor Green
}

exit 0
