#!/usr/bin/env sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_ROOT"

usage() {
  cat <<'EOF'
用法：./scripts/bootstrap.sh [--dev] [--help]

默认仅安装运行依赖；--dev 额外安装测试、Ruff 和 Pyright 等开发工具。
EOF
}

INSTALL_DEV=false
if [ "$#" -gt 1 ]; then
  echo "错误：参数过多。请运行 ./scripts/bootstrap.sh --help 查看用法。" >&2
  exit 2
fi
if [ "$#" -eq 1 ]; then
  case "$1" in
    --dev)
      INSTALL_DEV=true
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      echo "错误：未知参数：$1。请运行 ./scripts/bootstrap.sh --help 查看用法。" >&2
      exit 2
      ;;
  esac
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "错误：未找到 uv。请先安装：https://docs.astral.sh/uv/" >&2
  exit 1
fi

echo "[1/4] 检查 Python 3.12"
PYTHON_BIN=$(uv python find 3.12)
"$PYTHON_BIN" -c 'import sys; assert sys.version_info[:2] == (3, 12), sys.version'

if [ "$INSTALL_DEV" = true ]; then
  echo "[2/4] 按锁文件同步运行依赖和开发依赖"
  uv sync --frozen --python "$PYTHON_BIN"
else
  echo "[2/4] 按锁文件同步运行依赖（不安装开发工具）"
  uv sync --frozen --no-dev --python "$PYTHON_BIN"
fi

.venv/bin/python -c 'import sys; assert sys.version_info[:2] == (3, 12), sys.version'

if [ ! -f .env ]; then
  cp .env.example .env
  echo "[3/4] 已从 .env.example 创建 .env"
else
  echo "[3/4] 保留已有 .env"
fi

mkdir -p data logs output
DATABASE_FILE=data/intelligence.db
DATABASE_URL=sqlite:///data/intelligence.db

if [ -e "$DATABASE_FILE" ]; then
  echo "[4/4] 升级已有数据库并同步 canonical 来源目录（不会清空数据）"
else
  echo "[4/4] 初始化数据库并同步 canonical 来源目录"
fi
AIM_DATABASE_URL="$DATABASE_URL" .venv/bin/python -m alembic upgrade head
AIM_DATABASE_URL="$DATABASE_URL" .venv/bin/python -m app.cli sources sync-catalog --reconcile

echo "初始化完成。运行 ./scripts/run.sh 后访问 http://127.0.0.1:8000"
