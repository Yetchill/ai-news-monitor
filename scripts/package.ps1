param(
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "未找到 uv；Windows 打包必须从 uv.lock 同步的开发环境执行。"
}

$Version = uv run --frozen python -c "import tomllib; from pathlib import Path; print(tomllib.loads(Path('pyproject.toml').read_text(encoding='utf-8'))['project']['version'])"
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($Version)) {
    throw "无法从 pyproject.toml 读取唯一发布版本。"
}

Write-Host "[1/5] 扫描已跟踪的发布源输入（不读取本地 .env 或数据）"
uv run --frozen python scripts/inspect_release_artifact.py --source-root $ProjectRoot
if ($LASTEXITCODE -ne 0) { throw "发布源输入安全检查失败" }

Write-Host "[2/5] 构建 PyInstaller onedir（版本 $Version）"
uv run --frozen pyinstaller --noconfirm --clean `
    --distpath "$ProjectRoot/dist" `
    --workpath "$ProjectRoot/build/pyinstaller" `
    "$ProjectRoot/packaging/ai_intelligence_monitor.spec"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败" }

$Bundle = Join-Path $ProjectRoot "dist/AIIntelligenceMonitor"
$InspectArgs = @(
    "scripts/inspect_release_artifact.py",
    "--path", $Bundle,
    "--require", "alembic.ini",
    "--require", "app/config/source_catalog.yaml",
    "--require", "app/config/classification_rules.yaml",
    "--require", "app/web/templates/base.html",
    "--require", "app/web/static/app.js",
    "--require", "app/storage/migrations/versions/db0caa03a995_create_initial_schema.py"
)
Write-Host "[3/5] 检查 onedir 内容、密钥和运行时文件"
uv run --frozen python @InspectArgs
if ($LASTEXITCODE -ne 0) { throw "onedir artifact 检查失败" }

if ($SkipInstaller) {
    Write-Host "[4/5] 已跳过 Inno Setup；onedir 可用于打包集成测试"
    exit 0
}

$Iscc = Get-Command iscc.exe -ErrorAction SilentlyContinue
if ($null -eq $Iscc) {
    $Candidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
    ) | Where-Object { $_ -and (Test-Path $_) }
    if ($Candidates.Count -eq 0) {
        throw "未找到 Inno Setup 6（ISCC.exe）。请安装后重试。"
    }
    $IsccPath = $Candidates[0]
} else {
    $IsccPath = $Iscc.Source
}

Write-Host "[4/5] 构建 per-user Inno Setup 安装包"
& $IsccPath "/DMyAppVersion=$Version" "/DSourceDir=$Bundle" "packaging/windows-installer.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup 构建失败" }

$Installer = Join-Path $ProjectRoot "artifacts/AI-Intelligence-Monitor-Setup-$Version-x64.exe"
$ShaFile = Join-Path $ProjectRoot "artifacts/SHA256SUMS.txt"
Write-Host "[5/5] 检查安装包并生成 SHA-256"
uv run --frozen python scripts/inspect_release_artifact.py --path $Installer --sha256-out $ShaFile
if ($LASTEXITCODE -ne 0) { throw "installer artifact 检查失败" }
Write-Host "已生成：$Installer"
