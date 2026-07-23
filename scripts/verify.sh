#!/usr/bin/env sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_ROOT"

if ! command -v uv >/dev/null 2>&1; then
  echo "错误：未找到 uv。" >&2
  exit 1
fi

VERIFY_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/aim-public-verify.XXXXXX")
cleanup() {
  rm -rf -- "$VERIFY_ROOT"
}
trap cleanup EXIT HUP INT TERM

DATABASE_URL="sqlite:///$VERIFY_ROOT/verify.db"
export AIM_DATABASE_URL="$DATABASE_URL"
export AIM_LOG_DIR="$VERIFY_ROOT/logs"
export AIM_CLASSIFIER_MODE=rule
export AIM_LLM_API_KEY=
export PYTHONDONTWRITEBYTECODE=1
export RUFF_NO_CACHE=1

echo "[1/6] 检查 Python 3.12"
uv run --frozen python -c 'import sys; assert sys.version_info[:2] == (3, 12), sys.version; print(sys.version)'

echo "[2/6] 在临时数据库执行 migration"
uv run --frozen alembic upgrade head

echo "[3/6] Reconcile canonical 来源目录"
uv run --frozen python -m app.cli sources sync-catalog --reconcile

echo "[4/6] 验证来源数量（总计/监控中/候选 = 26/18/8）"
uv run --frozen python -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); counts=c.execute("select count(*), sum(lifecycle_state = \"active\"), sum(lifecycle_state = \"candidate\") from sources").fetchone(); assert counts == (26, 18, 8), counts; print(*counts)' "$VERIFY_ROOT/verify.db"

echo "[5/6] 运行非网络测试与静态检查"
uv run --frozen python -m pytest -m "not network" -p no:cacheprovider
uv run --frozen ruff check --no-cache app/ tests/
uv run --frozen pyright app/ tests/

echo "[6/6] 验证完成；临时数据库将在退出时删除"
