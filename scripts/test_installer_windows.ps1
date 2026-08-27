param(
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath
)

$ErrorActionPreference = "Stop"

function Wait-InstalledDesktop {
    param([System.Diagnostics.Process]$Process, [string]$StateFile)

    $deadline = (Get-Date).AddSeconds(45)
    while ((Get-Date) -lt $deadline) {
        if ($Process.HasExited) { throw "已安装程序在就绪前退出，exit=$($Process.ExitCode)" }
        if (Test-Path $StateFile) {
            try {
                $state = Get-Content $StateFile -Raw | ConvertFrom-Json
                $response = Invoke-WebRequest -Uri "http://127.0.0.1:$($state.port)/healthz" -UseBasicParsing
                if ($response.StatusCode -eq 200) { return }
            } catch {
                # Wait for the desktop runtime to finish binding and migrations.
            }
        }
        Start-Sleep -Milliseconds 250
    }
    throw "已安装程序未在 45 秒内就绪"
}

function Stop-InstalledDesktop {
    param([System.Diagnostics.Process]$Process, [string]$ShutdownFile, [string]$StateFile)

    New-Item -ItemType File -Path $ShutdownFile -Force | Out-Null
    if (-not $Process.WaitForExit(20000)) {
        Stop-Process -Id $Process.Id -Force
        throw "已安装程序未响应 test-only graceful shutdown"
    }
    if ((Test-Path $ShutdownFile) -or (Test-Path $StateFile)) {
        throw "已安装程序没有完整清理 test-only desktop 状态"
    }
}

$Installer = (Resolve-Path $InstallerPath).Path
$InstallDir = Join-Path $env:LOCALAPPDATA "Programs\AI Intelligence Monitor"
$Executable = Join-Path $InstallDir "AI 情报助手.exe"
$Uninstaller = Join-Path $InstallDir "unins000.exe"
$AppData = Join-Path $env:LOCALAPPDATA "AIIntelligenceMonitor"
$StateFile = Join-Path $AppData "desktop-instance.json"
$ShutdownFile = Join-Path $AppData "installer-test-shutdown"
$Marker = Join-Path $AppData "installer-lifecycle-marker.txt"
$DesktopShortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "AI 情报助手.lnk"
$StartMenuShortcut = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\AI 情报助手\AI 情报助手.lnk"
$UninstallRoot = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall"
$UninstallKey = $null
$PreexistingAppDataBackup = $null
if (Test-Path $AppData) {
    if ($env:GITHUB_ACTIONS -ne "true") {
        throw "installer lifecycle test requires a clean AppData directory: $AppData"
    }
    $PreexistingAppDataBackup = Join-Path $env:LOCALAPPDATA `
        ("AIIntelligenceMonitor-preexisting-" + [guid]::NewGuid().ToString("N"))
    $existingNames = (Get-ChildItem $AppData -Force | Select-Object -ExpandProperty Name) -join ", "
    Write-Host "Preserving pre-existing runner AppData entries: $existingNames"
    Move-Item -Path $AppData -Destination $PreexistingAppDataBackup
}

$previousDataDir = $env:AIM_DATA_DIR
$previousTestMode = $env:AIM_DESKTOP_TEST_MODE
$previousShutdownFile = $env:AIM_DESKTOP_SHUTDOWN_FILE
$previousBrowser = $env:AIM_DESKTOP_OPEN_BROWSER
$env:AIM_DATA_DIR = $null
$env:AIM_DESKTOP_TEST_MODE = "1"
$env:AIM_DESKTOP_SHUTDOWN_FILE = $ShutdownFile
$env:AIM_DESKTOP_OPEN_BROWSER = "0"
$process = $null

try {
    $install = Start-Process -FilePath $Installer -ArgumentList "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-" -PassThru -Wait
    if ($install.ExitCode -ne 0 -or -not (Test-Path $Executable)) { throw "per-user installer failed" }
    $process = Start-Process -FilePath $Executable -PassThru
    Wait-InstalledDesktop -Process $process -StateFile $StateFile
    Stop-InstalledDesktop -Process $process -ShutdownFile $ShutdownFile -StateFile $StateFile
    $process = $null
    if (-not (Test-Path (Join-Path $AppData "intelligence.db"))) { throw "未生成 AppData 数据库" }
    Set-Content -Path $Marker -Value "installer-lifecycle-marker-v1" -NoNewline
    if (-not (Test-Path $DesktopShortcut) -or -not (Test-Path $StartMenuShortcut)) {
        throw "installer did not create the desktop and Start Menu shortcuts"
    }
    $UninstallKey = Get-ChildItem $UninstallRoot | Where-Object {
        (Get-ItemProperty $_.PSPath).DisplayName -eq "AI 情报助手"
    } | Select-Object -First 1
    if ($null -eq $UninstallKey) { throw "installer did not create the HKCU uninstall registry key" }

    $uninstall = Start-Process -FilePath $Uninstaller -ArgumentList "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART" -PassThru -Wait
    if ($uninstall.ExitCode -ne 0 -or (Test-Path $InstallDir)) { throw "uninstaller failed" }
    if (-not (Test-Path $Marker) -or (Get-Content $Marker -Raw) -ne "installer-lifecycle-marker-v1") {
        throw "卸载错误删除或改变了 AppData 持久化标记"
    }
    if ((Test-Path $DesktopShortcut) -or (Test-Path $StartMenuShortcut)) {
        throw "uninstaller did not remove application shortcuts"
    }
    if (Test-Path $UninstallKey.PSPath) { throw "uninstaller did not remove its HKCU registry key" }

    $reinstall = Start-Process -FilePath $Installer -ArgumentList "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-" -PassThru -Wait
    if ($reinstall.ExitCode -ne 0 -or -not (Test-Path $Executable)) { throw "reinstall failed" }
    $ShutdownFile = Join-Path $AppData "installer-test-shutdown-reinstall"
    $env:AIM_DESKTOP_SHUTDOWN_FILE = $ShutdownFile
    $process = Start-Process -FilePath $Executable -PassThru
    Wait-InstalledDesktop -Process $process -StateFile $StateFile
    if ((Get-Content $Marker -Raw) -ne "installer-lifecycle-marker-v1") {
        throw "重装后没有保留 AppData 持久化标记"
    }
    Stop-InstalledDesktop -Process $process -ShutdownFile $ShutdownFile -StateFile $StateFile
    $process = $null
    Write-Host "per-user install, uninstall, data preservation, and reinstall passed"
} finally {
    if ($null -ne $process -and -not $process.HasExited) { Stop-Process -Id $process.Id -Force }
    $env:AIM_DATA_DIR = $previousDataDir
    $env:AIM_DESKTOP_TEST_MODE = $previousTestMode
    $env:AIM_DESKTOP_SHUTDOWN_FILE = $previousShutdownFile
    $env:AIM_DESKTOP_OPEN_BROWSER = $previousBrowser
    if ($null -ne $PreexistingAppDataBackup) {
        if (Test-Path $AppData) {
            $GeneratedAppData = Join-Path $env:LOCALAPPDATA `
                ("AIIntelligenceMonitor-installer-test-" + [guid]::NewGuid().ToString("N"))
            Move-Item -Path $AppData -Destination $GeneratedAppData
            Write-Host "Retained generated installer test data at $GeneratedAppData"
        }
        Move-Item -Path $PreexistingAppDataBackup -Destination $AppData
        Write-Host "Restored the pre-existing runner AppData directory"
    }
}
