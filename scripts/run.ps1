$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$VenvPython = Join-Path $ProjectRoot ".venv/Scripts/python.exe"
if (-not (Test-Path $VenvPython)) { throw "未找到项目虚拟环境。请先运行 .\scripts\bootstrap.ps1。" }
& $VenvPython -c "import sys; assert sys.version_info[:2] == (3, 12), sys.version"
if ($LASTEXITCODE -ne 0) { throw "项目虚拟环境不是 Python 3.12" }

$BindHost = if ($env:AIM_HOST) { $env:AIM_HOST } else { "127.0.0.1" }
$BindPort = if ($env:AIM_PORT) { $env:AIM_PORT } else { "8000" }

Write-Host "启动 AI 资讯监控：http://${BindHost}:$BindPort"
& $VenvPython -m uvicorn "app.web.app:create_app" --factory --host $BindHost --port $BindPort
if ($LASTEXITCODE -ne 0) { throw "Web 服务异常退出" }
