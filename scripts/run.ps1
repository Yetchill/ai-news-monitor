$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "未找到 uv。请先运行 bootstrap 脚本要求的准备步骤。"
}

$BindHost = if ($env:AIM_HOST) { $env:AIM_HOST } else { "127.0.0.1" }
$BindPort = if ($env:AIM_PORT) { $env:AIM_PORT } else { "8000" }

Write-Host "启动 AI 资讯监控：http://${BindHost}:$BindPort"
uv run --frozen uvicorn "app.web.app:create_app" --factory --host $BindHost --port $BindPort
if ($LASTEXITCODE -ne 0) { throw "Web 服务异常退出" }
