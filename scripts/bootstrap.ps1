$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$InstallDev = $false
if ($args.Count -gt 1) {
    throw "参数过多。请运行 .\scripts\bootstrap.ps1 --help 查看用法。"
}
if ($args.Count -eq 1) {
    switch ($args[0]) {
        "--dev" { $InstallDev = $true }
        "--help" {
            Write-Host "用法：.\scripts\bootstrap.ps1 [--dev] [--help]"
            Write-Host "默认仅安装运行依赖；--dev 额外安装测试、Ruff 和 Pyright 等开发工具。"
            exit 0
        }
        default { throw "未知参数：$($args[0])。请运行 .\scripts\bootstrap.ps1 --help 查看用法。" }
    }
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "未找到 uv。请先安装：https://docs.astral.sh/uv/"
}

Write-Host "[1/4] 检查 Python 3.12"
$PythonBin = (uv python find 3.12)
if ($LASTEXITCODE -ne 0) { throw "未找到 Python 3.12" }
& $PythonBin -c "import sys; assert sys.version_info[:2] == (3, 12), sys.version"
if ($LASTEXITCODE -ne 0) { throw "Python 版本检查失败" }

if ($InstallDev) {
    Write-Host "[2/4] 按锁文件同步运行依赖和开发依赖"
    uv sync --frozen --python $PythonBin
} else {
    Write-Host "[2/4] 按锁文件同步运行依赖（不安装开发工具）"
    uv sync --frozen --no-dev --python $PythonBin
}
if ($LASTEXITCODE -ne 0) { throw "依赖同步失败" }
$VenvPython = Join-Path $ProjectRoot ".venv/Scripts/python.exe"
& $VenvPython -c "import sys; assert sys.version_info[:2] == (3, 12), sys.version"
if ($LASTEXITCODE -ne 0) { throw "项目虚拟环境不是 Python 3.12" }

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
    Write-Host "[4/4] 升级已有数据库并同步 canonical 来源目录（不会清空数据）"
} else {
    Write-Host "[4/4] 初始化数据库并同步 canonical 来源目录"
}
$env:AIM_DATABASE_URL = $DatabaseUrl
& $VenvPython -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw "数据库 migration 失败" }
& $VenvPython -m app.cli sources sync-catalog --reconcile
if ($LASTEXITCODE -ne 0) { throw "来源目录同步失败" }

Write-Host "初始化完成。运行 .\scripts\run.ps1 后访问 http://127.0.0.1:8000"
