# AI Intelligence Monitor

一个本地优先的 AI 行业资讯监控工具：管理可信来源、执行采集与准入、按规则分类、人工复核，并在浏览器中筛选、标记、导出和审计更新记录。

默认使用纯规则模式，不需要 API Key，也不会自动调用外部 AI。DeepSeek、OpenAI、OpenRouter 和自定义 OpenAI-compatible 服务是显式启用的可选能力，分类与总结可分别选择服务商和模型。

## 功能概览

- 26 条 canonical 来源目录：18 条 active、8 条 candidate；
- RSS、HTML 列表、公开 JSON、GitHub Release 和单页更新日志采集器；
- 内容准入、去重、规则分类、人工分类、可信状态和审核状态；
- 资讯搜索、组合筛选、已读/收藏、卡片/紧凑视图和批量操作；
- 可选 AI 分类、AI 总结和任务记录，支持快速、智能、精确三种批量分类模式；
- 来源发现、预览、候选激活、启停和运行诊断；
- Excel、Word 导出，定时更新和完整 CrawlRun 审计；
- SQLite 本地存储，FastAPI + Jinja2 服务端页面。

## 本次更新

- Windows 运行数据采用统一的用户目录策略；未显式配置时使用 `%LOCALAPPDATA%\AIIntelligenceMonitor`，并增加同一数据库的单实例保护；
- 启动时自动检查并升级数据库，SQLite 启用 WAL、外键约束和忙等待，降低桌面端并发读写时的锁冲突；
- AI 工作流支持独立的分类/总结服务商、后台任务进度、失败重试，以及快速、智能、精确三种分类策略；
- 网络请求增加响应体大小限制和连接复用，错误信息会在写入日志或页面前脱敏；发布数据库副本时可清除其中保存的 API Key。

## 界面截图

![资讯首页](docs/screenshots/home-1440x900.png)

| 来源管理 | 更新记录 |
|---|---|
| ![来源管理](docs/screenshots/sources-1440x900.png) | ![更新记录](docs/screenshots/runs-1440x900.png) |

## 技术栈

- Python 3.12、uv、pytest、Ruff、Pyright；
- FastAPI、Uvicorn、Jinja2、原生 JavaScript/CSS；
- SQLAlchemy 2、SQLite、Alembic；
- httpx、Beautiful Soup、lxml、feedparser；
- openpyxl、python-docx；
- Pydantic Settings 与 YAML 配置。

## 普通用户安装

安装 [Python 3.12](https://www.python.org/) 和 [uv](https://docs.astral.sh/uv/)，然后执行：

```bash
git clone https://github.com/Yetchill/ai-news-monitor.git ai-intelligence-monitor
cd ai-intelligence-monitor
./scripts/bootstrap.sh
./scripts/run.sh
```

默认 bootstrap 只安装 Web 服务、数据库、采集、解析和导出所需的运行依赖，不安装
pytest、Ruff、Pyright、Node.js wheel 或类型存根。浏览 `http://127.0.0.1:8000/`。
### Windows 部署

先安装 64 位 Python 3.12、Git 和 uv，并在 PowerShell 中确认 `py -3.12 --version`、`git --version`、`uv --version` 均可运行。随后执行：

```powershell
git clone https://github.com/Yetchill/ai-news-monitor.git ai-intelligence-monitor
Set-Location ai-intelligence-monitor
.\scripts\bootstrap.ps1
.\scripts\run.ps1
```

打开 `http://127.0.0.1:8000/`，在运行窗口按 `Ctrl+C` 停止。服务默认只监听本机；如需更换端口，可先运行 `$env:AIM_PORT = "8765"`。首次安装、拉取新版本或依赖发生变化后可再次执行 `bootstrap.ps1`，脚本会保留已有 `.env` 和数据库，只做幂等迁移与来源目录对账。

源码部署由 `.env` 明确使用仓库内的 `data/intelligence.db` 和 `logs/`，便于整体备份；如果未设置这些路径，Windows 运行时会把数据库、日志和导出统一放到 `%LOCALAPPDATA%\AIIntelligenceMonitor`。可用 `AIM_DATA_DIR` 指定其他用户可写目录。不要把仓库或数据目录放在需要管理员权限的位置，也不要同时启动两个指向同一数据库的服务实例。

bootstrap 只在 `.env` 不存在时从 `.env.example` 创建配置；已有 `.env` 会原样保留。每次运行都会幂等执行数据库迁移和 canonical 来源目录对账，不会删除、清空或重建已有 `data/intelligence.db`。启动服务不会自动采集；首次获得资讯需在来源页面显式更新。macOS/Linux 可运行 `.venv/bin/python -m app.cli update`，Windows 可运行 `.\.venv\Scripts\python.exe -m app.cli update`。该操作会访问来源网站并写入当前数据库。

## 开发者安装

开发者使用 `--dev` 安装锁文件中的完整开发依赖：

```bash
./scripts/bootstrap.sh --dev
./scripts/run.sh
```

Windows PowerShell 对应使用 `./scripts/bootstrap.ps1 --dev`。

可用 `./scripts/bootstrap.sh --help` 查看参数。普通模式与开发模式都可重复执行；从普通模式切换到开发模式会补齐开发工具，再次执行普通模式会按锁文件移除开发组。

## 默认规则模式与可选 AI

`.env.example` 默认 `AIM_CLASSIFIER_MODE=rule` 且 Key 为空。规则分类、本地 Web、来源同步和非网络测试都不需要 AI。

若要启用 AI，请先阅读[用户指南](docs/USER_GUIDE.md#ai-分类和总结)和[安全说明](SECURITY.md)。可分别配置分类与总结使用的服务商和模型，并按成本与准确率选择“快速”“智能”或“精确”模式。API Key 可通过 `.env` 或 AI 页面配置；AI 页面保存的 Key 位于本地 SQLite，界面只显示掩码，但当前数据库字段不是加密保险库。请保护数据库文件和备份，对外分发数据库前务必使用清理副本。

## 文档

- [用户指南](docs/USER_GUIDE.md)
- [技术架构](docs/TECHNICAL_ARCHITECTURE.md)
- [从零复现](docs/REPRODUCIBILITY.md)
- [故障排查](docs/TROUBLESHOOTING.md)
- [第三方依赖许可证审查](docs/THIRD_PARTY_LICENSES.md)
- [安全策略](SECURITY.md)
- [版本记录](CHANGELOG.md)

## 项目结构

```text
app/                    应用、领域、服务、采集器、存储与 Web
app/config/             分类配置和唯一 source_catalog.yaml
app/storage/migrations/ Alembic migration
tests/                  离线单元测试、固定样本和可选网络测试
docs/                   用户、架构、复现和故障排查文档
scripts/                macOS/Linux 与 Windows 启动、验证脚本
data/                   本地数据库目录（数据库文件被忽略）
output/                 导出目录（生成文件被忽略）
```

## 测试与质量检查

```bash
./scripts/verify.sh

# 或逐项执行
.venv/bin/python -m pytest -m "not network"
.venv/bin/ruff check app/ tests/
.venv/bin/pyright app/ tests/
```

默认测试不访问公网、不调用真实 AI，也不写 `data/intelligence.db`。

## 许可证

仓库当前使用保留所有权利的临时许可证声明。公开发布前，版权所有者需要明确选择并授权一个开源许可证；详见 [LICENSE](LICENSE)。第三方依赖继续受各自许可证约束。
