# 用户指南

## 1. 准备环境

需要 Python 3.12、Git 和 uv。建议至少预留 1 GB 磁盘空间给虚拟环境、数据库、日志和导出。

### macOS

```bash
brew install python@3.12 uv git
python3.12 --version
uv --version
```

### Linux

使用发行版包管理器或 python.org 安装 Python 3.12，再按 uv 官方方式安装。确认命令：

```bash
python3.12 --version
uv --version
git --version
```

### Windows

从 python.org 安装 Python 3.12 并勾选 PATH，安装 Git 和 uv，然后在 PowerShell 中检查：

```powershell
py -3.12 --version
uv --version
git --version
```

## 2. 首次安装

macOS/Linux：

```bash
git clone <YOUR_REPOSITORY_URL> ai-intelligence-monitor
cd ai-intelligence-monitor
./scripts/bootstrap.sh
```

Windows PowerShell：

```powershell
git clone <YOUR_REPOSITORY_URL> ai-intelligence-monitor
Set-Location ai-intelligence-monitor
./scripts/bootstrap.ps1
```

脚本会执行 `uv sync --frozen`、复制缺失的 `.env`、创建目录、迁移一个不存在的新数据库，并运行 canonical source reconcile。已有数据库不会被覆盖。

手工等价步骤：

```bash
uv sync --frozen
cp .env.example .env
mkdir -p data logs output
AIM_DATABASE_URL=sqlite:///data/intelligence.db uv run --frozen alembic upgrade head
AIM_DATABASE_URL=sqlite:///data/intelligence.db uv run --frozen python -m app.cli sources sync-catalog --reconcile
```

Windows 可用 `Copy-Item .env.example .env`，或直接使用 `bootstrap.ps1`。

## 3. 启动和停止 Web

```bash
./scripts/run.sh
```

Windows：

```powershell
./scripts/run.ps1
```

浏览 `http://127.0.0.1:8000/`。终端按 `Ctrl+C` 停止服务。默认只监听本机回环地址；不要在没有认证和 CSRF 防护时绑定公网地址。

## 4. 初始化来源目录

Alembic 只创建结构，不自动插入业务来源。初始化或修复来源集合需要显式执行：

```bash
AIM_DATABASE_URL=sqlite:///data/intelligence.db \
  uv run --frozen python -m app.cli sources sync-catalog --reconcile
```

预期结果为 26 条：18 active、8 candidate。reconcile 以 slug 为主键；无历史引用的旧 preset 可安全移除，有资讯或运行引用的旧来源只会停用并保留关联。普通 GET 页面不会修改数据库。

## 5. 更新资讯

来源页可以更新全部启用来源，也可在来源详情页更新单个来源。CLI：

```bash
uv run --frozen python -m app.cli update
uv run --frozen python -m app.cli update --source-id 1
```

这些操作会访问公网并写数据库。先确认数据库路径、网络使用和来源条款。candidate 不参加批量更新；需要先预览并显式激活。

## 6. 资讯操作

- 顶部筛选支持关键词、分类、来源、阅读状态和快捷时间；“更多筛选”包含收藏、日期、可信状态、审核状态、待分类和每页数量。
- 已读、未读、收藏和人工分类都会持久化到 SQLite；`False` 的未读筛选与未提供筛选是不同状态。
- 卡片和紧凑视图使用相同的后端查询结果；视图选择保存在浏览器 localStorage。
- 标题或“查看原文”在新标签打开真实来源 URL；无 URL 时不可点击。
- 单条和批量操作完成后，当前筛选页面会反映最新状态。

## 7. AI 分类和总结

默认关闭 AI，不需要 Key。规则分类仍可用于采集流水线。

启用步骤：

1. 打开“AI”页面；
2. 设置 OpenAI-compatible Base URL、模型和 API Key；
3. 先使用“测试连接”；
4. 选择手动或自动分类/总结模式；
5. 保存后执行单条或批量任务。

也可在私有 `.env` 中设置 `AIM_LLM_BASE_URL`、`AIM_LLM_API_KEY`、`AIM_LLM_MODEL`。不要提交 `.env`。AI 页面只回显掩码，但保存的 Key 位于本地 SQLite 的普通字段；请保护数据库和备份。失败任务会记录安全化错误，可在 AI 页面检查。

## 8. 来源管理

- “监控中”显示 active 来源，“候选”显示尚未激活来源。
- 新来源先经过 URL 安全检查、采集器识别和最多 10 条预览；保存时不信任浏览器回传的采集器配置。
- candidate 必须预览并确认激活，受限或需专用采集器的来源不能伪装成可用。
- 来源详情包含最近运行、最近资讯、成功/失败/拒绝统计和错误摘要。
- 停用不会删除历史资讯。不要直接编辑 SQLite 绕过生命周期约束。

## 9. 导出

资讯页可按当前筛选导出 Excel 或 Word，导出全部匹配记录而不是仅当前页。CLI 示例：

```bash
uv run --frozen python -m app.cli export excel --output output/report.xlsx
uv run --frozen python -m app.cli export word --output output/report.docx
```

已有目标不会静默覆盖；请使用新文件名，或在确认后使用 CLI 支持的显式覆盖选项。

## 10. 定时更新

设置页可配置时区、星期和时间。调度器只在 Web 进程运行时有效，关机期间不补跑。不要同时运行多个 Web/CLI 调度进程访问同一个 SQLite。

## 11. 数据备份

先停止 Web 和任何 CLI 更新，再复制数据库及可选 WAL 文件：

```bash
cp data/intelligence.db data/intelligence-backup-$(date +%Y%m%d-%H%M%S).db
```

更稳妥的在线备份可使用 SQLite `.backup` 命令。恢复前先备份当前库，不要对唯一正式数据库试验 migration 或 purge。

Windows 可在服务停止后使用 `Copy-Item` 并给文件名加时间。数据库可能包含 AI Key，备份必须按秘密处理。

## 12. 常见问题

- 页面没有资讯：来源目录只提供配置，首次必须显式更新；检查 `/runs` 错误和来源状态。
- 分类筛选 400：使用页面提供的稳定 option value，不手工提交中文显示文本。
- 已读筛选为空：确认库中确有已读记录，可先在资讯页标记一条。
- AI 不工作：默认关闭；确认 Key、Base URL、模型和连接测试。
- 端口被占用：设置 `AIM_PORT=8765 ./scripts/run.sh`，Windows 为 `$env:AIM_PORT=8765`。
- 更详细处理见[故障排查](TROUBLESHOOTING.md)。
