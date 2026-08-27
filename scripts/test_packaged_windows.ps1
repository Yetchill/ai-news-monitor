param(
    [string]$BundlePath = "dist/AIIntelligenceMonitor",
    [switch]$ExerciseFeatureRoutes
)

$ErrorActionPreference = "Stop"

function Wait-DesktopReady {
    param([System.Diagnostics.Process]$Process, [string]$StateFile)

    $deadline = (Get-Date).AddSeconds(45)
    while ((Get-Date) -lt $deadline) {
        if ($Process.HasExited) {
            throw "打包程序在就绪前退出，exit=$($Process.ExitCode)"
        }
        if (Test-Path $StateFile) {
            try {
                $state = Get-Content $StateFile -Raw | ConvertFrom-Json
                $url = "http://127.0.0.1:$($state.port)/"
                $response = Invoke-WebRequest -Uri "${url}healthz" -UseBasicParsing -TimeoutSec 3
                if ($response.StatusCode -eq 200) {
                    return [pscustomobject]@{ Url = $url; State = $state }
                }
            } catch {
                # Startup races are expected while the bundled interpreter initializes.
            }
        }
        Start-Sleep -Milliseconds 250
    }
    throw "打包程序未在 45 秒内通过健康检查"
}

function Stop-DesktopGracefully {
    param(
        [System.Diagnostics.Process]$Process,
        [string]$ShutdownFile,
        [string]$StateFile
    )

    New-Item -ItemType File -Path $ShutdownFile -Force | Out-Null
    if (-not $Process.WaitForExit(20000)) {
        throw "test-only graceful shutdown 未在 20 秒内退出"
    }
    if (Test-Path $ShutdownFile) { throw "test-only shutdown 文件未被程序消费" }
    if (Test-Path $StateFile) { throw "优雅退出后 desktop-instance.json 仍存在" }
}

function Wait-FixtureReady {
    param([System.Diagnostics.Process]$Process, [string]$StateFile)

    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        if ($Process.HasExited) {
            throw "本机 fixture server 在就绪前退出，exit=$($Process.ExitCode)"
        }
        if (Test-Path $StateFile) {
            try {
                $state = Get-Content $StateFile -Raw | ConvertFrom-Json
                if ($state.ready -and $state.port -gt 0) {
                    $baseUrl = "http://127.0.0.1:$($state.port)"
                    $probe = Invoke-WebRequest -Uri "$baseUrl/state" -UseBasicParsing -TimeoutSec 3
                    if ($probe.StatusCode -eq 200) { return $baseUrl }
                }
            } catch {
                # The state file is replaced atomically, but the server may not accept yet.
            }
        }
        Start-Sleep -Milliseconds 100
    }
    throw "本机 fixture server 未在 30 秒内就绪"
}

function Wait-PackagedState {
    param(
        [string]$Verifier,
        [string]$Database,
        [string]$Phase,
        [int]$TimeoutSeconds = 45
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $lastOutput = "尚未执行验证"
    while ((Get-Date) -lt $deadline) {
        $output = & uv run --frozen python $Verifier --database $Database --phase $Phase 2>&1
        if ($LASTEXITCODE -eq 0) { return ($output -join [Environment]::NewLine) }
        $lastOutput = $output -join [Environment]::NewLine
        Start-Sleep -Milliseconds 500
    }
    throw "打包调度状态未在 $TimeoutSeconds 秒内满足断言：$lastOutput"
}

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Bundle = Join-Path $ProjectRoot $BundlePath
$Executable = Join-Path $Bundle "AI 情报助手.exe"
$FixtureScript = Join-Path $PSScriptRoot "packaged_fixture_server.py"
$SeedScript = Join-Path $PSScriptRoot "seed_packaged_test_data.py"
$Verifier = Join-Path $PSScriptRoot "verify_packaged_test_state.py"
if (-not (Test-Path $Executable)) { throw "未找到 Windows onedir 可执行文件：$Executable" }
foreach ($required in @($FixtureScript, $SeedScript, $Verifier)) {
    if (-not (Test-Path $required)) { throw "缺少 packaged integration helper：$required" }
}

$TestData = Join-Path ([System.IO.Path]::GetTempPath()) ("aim-packaged-" + [guid]::NewGuid().ToString("N"))
$Database = Join-Path $TestData "intelligence.db"
$StateFile = Join-Path $TestData "desktop-instance.json"
$ShutdownFile = Join-Path $TestData "request-graceful-shutdown-bootstrap"
$FixtureStateFile = Join-Path $TestData "fixture-state.json"
$FixtureStdout = Join-Path $TestData "fixture-stdout.log"
$FixtureStderr = Join-Path $TestData "fixture-stderr.log"
$ExcelPath = Join-Path $TestData "packaged-export.xlsx"
$WordPath = Join-Path $TestData "packaged-export.docx"
New-Item -ItemType Directory -Path $TestData | Out-Null

$previousDataDir = $env:AIM_DATA_DIR
$previousBrowser = $env:AIM_DESKTOP_OPEN_BROWSER
$previousTestMode = $env:AIM_DESKTOP_TEST_MODE
$previousShutdownFile = $env:AIM_DESKTOP_SHUTDOWN_FILE
$previousTestInterval = $env:AIM_DESKTOP_TEST_SCHEDULER_INTERVAL_SECONDS
$env:AIM_DATA_DIR = $TestData
$env:AIM_DESKTOP_OPEN_BROWSER = "0"
$env:AIM_DESKTOP_TEST_MODE = "1"
$env:AIM_DESKTOP_SHUTDOWN_FILE = $ShutdownFile
$env:AIM_DESKTOP_TEST_SCHEDULER_INTERVAL_SECONDS = "1"

$bootstrap = $null
$second = $null
$scheduled = $null
$restarted = $null
$fixtureServer = $null
$fixtureBaseUrl = $null
$primaryFailure = $false
$portBlocker = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 8000)

try {
    # First launch owns migrations and proves the packaged host does not require port 8000.
    $portBlocker.Start()
    $bootstrap = Start-Process -FilePath $Executable -PassThru
    $ready = Wait-DesktopReady -Process $bootstrap -StateFile $StateFile
    if ($ready.State.port -eq 8000) { throw "默认端口被占用时未回退到随机端口" }
    if (-not (Test-Path $Database)) { throw "打包程序未在 AIM_DATA_DIR 初始化用户数据库" }

    # A second launch must activate the first instance and leave its state intact.
    $second = Start-Process -FilePath $Executable -PassThru
    if (-not $second.WaitForExit(10000)) { throw "第二实例没有及时退出" }
    $stateAfterSecond = Get-Content $StateFile -Raw | ConvertFrom-Json
    if ($bootstrap.HasExited -or $stateAfterSecond.pid -ne $bootstrap.Id) {
        throw "第二实例意外替换或终止了首实例"
    }
    Stop-DesktopGracefully -Process $bootstrap -ShutdownFile $ShutdownFile -StateFile $StateFile
    $bootstrap = $null
    $portBlocker.Stop()

    # The source tree starts only a loopback deterministic server. The packaged
    # executable performs all crawler, scheduler, persistence, and AI work.
    $uvPath = (Get-Command uv).Source
    $fixtureArguments = @(
        "run", "--frozen", "python", "`"$FixtureScript`"",
        "--state-file", "`"$FixtureStateFile`""
    )
    $fixtureServer = Start-Process -FilePath $uvPath -ArgumentList $fixtureArguments `
        -WorkingDirectory $ProjectRoot -RedirectStandardOutput $FixtureStdout `
        -RedirectStandardError $FixtureStderr -PassThru
    $fixtureBaseUrl = Wait-FixtureReady -Process $fixtureServer -StateFile $FixtureStateFile

    & uv run --frozen python $SeedScript --database $Database --fixture-base-url $fixtureBaseUrl
    if ($LASTEXITCODE -ne 0) { throw "无法写入打包调度 fixture 数据" }

    # Restart after direct seeding: SchedulerService reads the persisted enabled
    # schedule during the packaged FastAPI lifespan and uses the test-only clock.
    $ShutdownFile = Join-Path $TestData "request-graceful-shutdown-integration"
    $env:AIM_DESKTOP_SHUTDOWN_FILE = $ShutdownFile
    $scheduled = Start-Process -FilePath $Executable -PassThru
    $ready = Wait-DesktopReady -Process $scheduled -StateFile $StateFile
    $home = Invoke-WebRequest -Uri $ready.Url -UseBasicParsing -TimeoutSec 5
    $static = Invoke-WebRequest -Uri "$($ready.Url)static/styles.css" -UseBasicParsing -TimeoutSec 5
    if ($home.Content -notmatch "<html" -or $static.Content.Length -lt 100) {
        throw "关键页面或静态资源没有由打包程序提供"
    }
    if ($ExerciseFeatureRoutes) {
        foreach ($path in @("sources", "runs", "settings", "ai")) {
            Invoke-WebRequest -Uri "$($ready.Url)$path" -UseBasicParsing -TimeoutSec 5 | Out-Null
        }
    }

    $stateSummary = Wait-PackagedState -Verifier $Verifier -Database $Database -Phase "integration"
    Write-Host $stateSummary
    $fixtureState = Invoke-RestMethod -Uri "$fixtureBaseUrl/state" -TimeoutSec 5
    if ($fixtureState.feed_requests -lt 6) { throw "fixture 未经历失败、恢复、新增和去重轮次" }
    if ($fixtureState.ai_requests -lt 1) { throw "打包 AI 任务未调用本机 fake endpoint" }
    if ($fixtureState.fake_total_tokens -ne 0 -or $fixtureState.external_service_requests -ne 0) {
        throw "fake AI fixture 报告了外部请求或非零 Token"
    }

    $excelResponse = Invoke-WebRequest -Method Post -Uri "$($ready.Url)exports/excel" `
        -UseBasicParsing -OutFile $ExcelPath -PassThru -TimeoutSec 20
    $wordResponse = Invoke-WebRequest -Method Post -Uri "$($ready.Url)exports/word" `
        -UseBasicParsing -OutFile $WordPath -PassThru -TimeoutSec 20
    foreach ($response in @($excelResponse, $wordResponse)) {
        if ($response.Headers["Content-Disposition"] -notmatch "attachment") {
            throw "打包导出路由未返回 attachment"
        }
    }
    if ((Get-Item $ExcelPath).Length -lt 1000 -or (Get-Item $WordPath).Length -lt 1000) {
        throw "打包导出文件大小异常"
    }
    & uv run --frozen python $Verifier --database $Database --phase integration `
        --xlsx $ExcelPath --docx $WordPath
    if ($LASTEXITCODE -ne 0) { throw "Office 文件重新打开验证失败" }

    Stop-DesktopGracefully -Process $scheduled -ShutdownFile $ShutdownFile -StateFile $StateFile
    $scheduled = $null
    & uv run --frozen python $Verifier --database $Database --phase integration `
        --xlsx $ExcelPath --docx $WordPath
    if ($LASTEXITCODE -ne 0) { throw "优雅退出后出现运行中任务或持久化损坏" }

    # Restart the same packaged executable and prove the persisted marker is
    # actually rendered from the retained database, then verify no stuck rows.
    $ShutdownFile = Join-Path $TestData "request-graceful-shutdown-restart"
    $env:AIM_DESKTOP_SHUTDOWN_FILE = $ShutdownFile
    $restarted = Start-Process -FilePath $Executable -PassThru
    $ready = Wait-DesktopReady -Process $restarted -StateFile $StateFile
    $restartHome = Invoke-WebRequest -Uri $ready.Url -UseBasicParsing -TimeoutSec 5
    if ($restartHome.Content -notmatch "打包集成持久化标记-PACKAGED_EXPORT_MARKER") {
        throw "重启后的打包页面未呈现持久化中文标记"
    }
    Stop-DesktopGracefully -Process $restarted -ShutdownFile $ShutdownFile -StateFile $StateFile
    $restarted = $null
    & uv run --frozen python $Verifier --database $Database --phase restarted `
        --xlsx $ExcelPath --docx $WordPath
    if ($LASTEXITCODE -ne 0) { throw "重启后持久化或任务终态验证失败" }
    Write-Host "Windows packaged crawler/scheduler/AI/export/restart integration passed: $($ready.Url)"
} catch {
    $primaryFailure = $true
    Write-Host "packaged integration primary failure: $($_.Exception.ToString())"
    foreach ($diagnostic in @(
        (Join-Path $TestData "logs/application.log"),
        $FixtureStdout,
        $FixtureStderr
    )) {
        if (Test-Path $diagnostic) {
            Write-Host "----- diagnostic: $diagnostic -----"
            Get-Content $diagnostic -Tail 300
        }
    }
    throw
} finally {
    $cleanupFailure = $false
    foreach ($process in @($bootstrap, $second, $scheduled, $restarted)) {
        if ($null -ne $process -and -not $process.HasExited) {
            New-Item -ItemType File -Path $ShutdownFile -Force | Out-Null
            if (-not $process.WaitForExit(20000)) {
                Stop-Process -Id $process.Id -Force
                $cleanupFailure = $true
            }
        }
    }
    $portBlocker.Stop()
    if ($null -ne $fixtureServer -and -not $fixtureServer.HasExited) {
        try {
            if ($null -ne $fixtureBaseUrl) {
                Invoke-WebRequest -Method Post -Uri "$fixtureBaseUrl/shutdown" `
                    -UseBasicParsing -TimeoutSec 3 | Out-Null
            }
            if (-not $fixtureServer.WaitForExit(5000)) {
                Stop-Process -Id $fixtureServer.Id -Force
                $cleanupFailure = $true
            }
        } catch {
            Stop-Process -Id $fixtureServer.Id -Force
            $cleanupFailure = $true
        }
    }
    $env:AIM_DATA_DIR = $previousDataDir
    $env:AIM_DESKTOP_OPEN_BROWSER = $previousBrowser
    $env:AIM_DESKTOP_TEST_MODE = $previousTestMode
    $env:AIM_DESKTOP_SHUTDOWN_FILE = $previousShutdownFile
    $env:AIM_DESKTOP_TEST_SCHEDULER_INTERVAL_SECONDS = $previousTestInterval
    if (Test-Path $TestData) { Remove-Item -Recurse -Force $TestData }
    if ($cleanupFailure) {
        if ($primaryFailure) {
            Write-Warning "cleanup required forced process termination after the primary failure"
        } else {
            throw "cleanup required a forced process termination"
        }
    }
}
