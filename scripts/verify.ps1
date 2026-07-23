$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "未找到 uv。"
}

$VerifyRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("aim-public-verify-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $VerifyRoot | Out-Null

try {
    $DatabasePath = (Join-Path $VerifyRoot "verify.db").Replace("\", "/")
    $env:AIM_DATABASE_URL = "sqlite:///$DatabasePath"
    $env:AIM_LOG_DIR = Join-Path $VerifyRoot "logs"
    $env:AIM_CLASSIFIER_MODE = "rule"
    $env:AIM_LLM_API_KEY = ""
    $env:PYTHONDONTWRITEBYTECODE = "1"
    $env:RUFF_NO_CACHE = "1"

    Write-Host "[1/6] 检查 Python 3.12"
    uv run --frozen python -c "import sys; assert sys.version_info[:2] == (3, 12), sys.version; print(sys.version)"
    if ($LASTEXITCODE -ne 0) { throw "Python 版本检查失败" }

    Write-Host "[2/6] 在临时数据库执行 migration"
    uv run --frozen alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw "数据库 migration 失败" }

    Write-Host "[3/6] Reconcile canonical 来源目录"
    uv run --frozen python -m app.cli sources sync-catalog --reconcile
    if ($LASTEXITCODE -ne 0) { throw "来源目录同步失败" }

    Write-Host "[4/6] 验证来源数量（总计/监控中/候选 = 26/18/8）"
    uv run --frozen python -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); counts=c.execute("select count(*), sum(lifecycle_state = ''active''), sum(lifecycle_state = ''candidate'') from sources").fetchone(); assert counts == (26, 18, 8), counts; print(*counts)' $DatabasePath
    if ($LASTEXITCODE -ne 0) { throw "来源数量验证失败" }

    Write-Host "[5/6] 运行非网络测试与静态检查"
    uv run --frozen python -m pytest -m "not network" -p no:cacheprovider
    if ($LASTEXITCODE -ne 0) { throw "测试失败" }
    uv run --frozen ruff check --no-cache app/ tests/
    if ($LASTEXITCODE -ne 0) { throw "Ruff 检查失败" }
    uv run --frozen pyright app/ tests/
    if ($LASTEXITCODE -ne 0) { throw "Pyright 检查失败" }

    Write-Host "[6/6] 验证完成；临时数据库将在退出时删除"
} finally {
    if (Test-Path $VerifyRoot) {
        Remove-Item -Recurse -Force $VerifyRoot
    }
}
