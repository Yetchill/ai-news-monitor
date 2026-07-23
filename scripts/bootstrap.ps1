$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "未找到 uv。请先安装：https://docs.astral.sh/uv/"
}

Write-Host "[1/4] 检查 Python 3.12"
$PythonBin = (uv python find 3.12)
if ($LASTEXITCODE -ne 0) { throw "未找到 Python 3.12" }
& $PythonBin -c "import sys; assert sys.version_info[:2] == (3, 12), sys.version"
if ($LASTEXITCODE -ne 0) { throw "Python 版本检查失败" }

Write-Host "[2/4] 按锁文件同步依赖"
uv sync --frozen
if ($LASTEXITCODE -ne 0) { throw "依赖同步失败" }

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host "[3/4] 已从 .env.example 创建 .env"
} else {
    Write-Host "[3/4] 保留已有 .env"
}

New-Item -ItemType Directory -Force data, logs, output | Out-Null
$DatabaseFile = Join-Path $ProjectRoot "data/intelligence.db"
$DatabaseUrl = "sqlite:///data/intelligence.db"

if (Test-Path $DatabaseFile) {
    Write-Host "[4/4] 检测到已有数据库，未覆盖：$DatabaseFile"
    Write-Host "如需升级现有数据库，请先备份，再手工运行 migration 与 catalog reconcile。"
} else {
    Write-Host "[4/4] 初始化数据库并同步 canonical 来源目录"
    $env:AIM_DATABASE_URL = $DatabaseUrl
    uv run --frozen alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw "数据库 migration 失败" }
    uv run --frozen python -m app.cli sources sync-catalog --reconcile
    if ($LASTEXITCODE -ne 0) { throw "来源目录同步失败" }
}

Write-Host "初始化完成。运行 .\scripts\run.ps1 后访问 http://127.0.0.1:8000"
