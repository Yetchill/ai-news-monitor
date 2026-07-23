# 从零复现

以下步骤以全新 clone、Python 3.12 和空数据库为前提，不复用开发者虚拟环境，不访问真实来源，不调用真实 AI。

## macOS / Linux

```bash
git clone <YOUR_REPOSITORY_URL> ai-intelligence-monitor
cd ai-intelligence-monitor

python3.12 --version
uv --version
uv sync --frozen

cp .env.example .env
mkdir -p data logs output
test ! -e data/intelligence.db

AIM_DATABASE_URL=sqlite:///data/intelligence.db \
  uv run --frozen alembic upgrade head

AIM_DATABASE_URL=sqlite:///data/intelligence.db \
  uv run --frozen python -m app.cli sources sync-catalog --reconcile
```

验证来源数量：

```bash
AIM_DATABASE_URL=sqlite:///data/intelligence.db uv run --frozen python -c '
from sqlalchemy import create_engine, text
engine = create_engine("sqlite:///data/intelligence.db")
with engine.connect() as connection:
    total, active, candidate = connection.execute(text("""
        SELECT count(*),
               sum(CASE WHEN lifecycle_state="active" THEN 1 ELSE 0 END),
               sum(CASE WHEN lifecycle_state="candidate" THEN 1 ELSE 0 END)
        FROM sources WHERE catalog_managed=1
    """)).one()
assert (total, active, candidate) == (26, 18, 8)
print(total, active, candidate)
engine.dispose()
'
```

运行离线质量检查：

```bash
uv run --frozen python -m pytest -m "not network"
uv run --frozen ruff check app/ tests/
uv run --frozen pyright app/ tests/
```

启动 Web：

```bash
uv run --frozen uvicorn "app.web.app:create_app" --factory \
  --host 127.0.0.1 --port 8000
```

另一个终端检查：

```bash
for path in / /ai /sources /settings /runs; do
  curl --fail --silent --show-error "http://127.0.0.1:8000${path}" >/dev/null
done
```

## Windows PowerShell

```powershell
git clone <YOUR_REPOSITORY_URL> ai-intelligence-monitor
Set-Location ai-intelligence-monitor
py -3.12 --version
uv --version
uv sync --frozen
Copy-Item .env.example .env
New-Item -ItemType Directory -Force data, logs, output | Out-Null
$env:AIM_DATABASE_URL = "sqlite:///data/intelligence.db"
uv run --frozen alembic upgrade head
uv run --frozen python -m app.cli sources sync-catalog --reconcile
./scripts/verify.ps1
./scripts/run.ps1
```

## 默认不调用真实 AI 的证明

- `.env.example` 的 Key 为空且 `AIM_CLASSIFIER_MODE=rule`；
- bootstrap、migration、catalog reconcile、pytest/Ruff/Pyright 和 Web GET 不执行 AI 请求；
- 非网络测试使用 fake/mock provider；
- 不点击更新按钮时 Web 启动也不抓取来源；
- AI 只有在用户配置 Key 并显式启用/触发后才访问 provider。

`scripts/verify.sh` / `verify.ps1` 使用系统临时目录中的唯一数据库，结束后删除，不触碰 `data/intelligence.db`。
