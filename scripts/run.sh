#!/usr/bin/env sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_ROOT"

VENV_PYTHON=.venv/bin/python
if [ ! -x "$VENV_PYTHON" ]; then
  echo "错误：未找到项目虚拟环境。请先运行 ./scripts/bootstrap.sh。" >&2
  exit 1
fi
"$VENV_PYTHON" -c 'import sys; assert sys.version_info[:2] == (3, 12), sys.version'

BIND_HOST=${AIM_HOST:-127.0.0.1}
BIND_PORT=${AIM_PORT:-8000}

echo "启动 AI 资讯监控： http://$BIND_HOST:$BIND_PORT"
exec "$VENV_PYTHON" -m uvicorn "app.web.app:create_app" --factory --host "$BIND_HOST" --port "$BIND_PORT"
