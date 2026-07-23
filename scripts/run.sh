#!/usr/bin/env sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_ROOT"

if ! command -v uv >/dev/null 2>&1; then
  echo "错误：未找到 uv。请先运行 bootstrap 脚本要求的准备步骤。" >&2
  exit 1
fi

BIND_HOST=${AIM_HOST:-127.0.0.1}
BIND_PORT=${AIM_PORT:-8000}

echo "启动 AI 资讯监控： http://$BIND_HOST:$BIND_PORT"
exec uv run --frozen uvicorn "app.web.app:create_app" --factory --host "$BIND_HOST" --port "$BIND_PORT"
