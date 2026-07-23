#!/usr/bin/env sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_ROOT"

if ! command -v uv >/dev/null 2>&1; then
  echo "错误：未找到 uv。请先安装：https://docs.astral.sh/uv/" >&2
  exit 1
fi

echo "[1/4] 检查 Python 3.12"
PYTHON_BIN=$(uv python find 3.12)
"$PYTHON_BIN" -c 'import sys; assert sys.version_info[:2] == (3, 12), sys.version'

echo "[2/4] 按锁文件同步依赖"
uv sync --frozen

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
  echo "[4/4] 检测到已有数据库，未覆盖：$DATABASE_FILE"
  echo "如需升级现有数据库，请先备份，再手工运行 migration 与 catalog reconcile。"
else
  echo "[4/4] 初始化数据库并同步 canonical 来源目录"
  AIM_DATABASE_URL="$DATABASE_URL" uv run --frozen alembic upgrade head
  AIM_DATABASE_URL="$DATABASE_URL" uv run --frozen python -m app.cli sources sync-catalog --reconcile
fi

echo "初始化完成。运行 ./scripts/run.sh 后访问 http://127.0.0.1:8000"
