# 故障排查

## Python 版本不对

项目要求 Python 3.12。执行 `python --version`、`python3.12 --version` 或 `py -3.12 --version`。删除错误环境前先确认它只属于当前 clone；然后重新运行 `uv sync --frozen`。

## uv 环境异常

先运行 `uv --version`、`uv python find 3.12` 和 `uv sync --frozen -v`。锁文件与 `pyproject.toml` 不一致时不要自行更新锁；确认 clone 完整。代理或证书导致下载失败时按组织网络策略配置 uv，不要关闭 TLS 验证。

## SQLite URL 斜杠

- 相对路径：`sqlite:///data/intelligence.db`；
- Unix 绝对路径：`sqlite:////absolute/path/intelligence.db`；
- Windows：推荐相对路径；绝对路径形如 `sqlite:///C:/work/data/intelligence.db`。

少一个斜杠可能连接到意外位置。启动前打印或检查 `AIM_DATABASE_URL`。

## migration 失败

确认数据库可写、不是目录、没有其他进程持锁，并运行：

```bash
AIM_DATABASE_URL=sqlite:///data/intelligence.db uv run --frozen alembic current
AIM_DATABASE_URL=sqlite:///data/intelligence.db uv run --frozen alembic upgrade head
```

不要修改已发布 migration，不要用删除正式数据库来“修复”。先备份，再针对错误 revision 诊断。

## source catalog 未同步

新 migration 数据库来源表为空是正常的。显式运行：

```bash
AIM_DATABASE_URL=sqlite:///data/intelligence.db \
  uv run --frozen python -m app.cli sources sync-catalog --reconcile
```

应报告 total 26、active 18、candidate 8。普通 `/sources` GET 不会隐式写库。

## 页面没有资讯

来源同步只创建配置，不抓取内容。确认 active 来源存在，然后从来源页显式更新，或执行 CLI update。查看 `/runs` 的来源错误、拒绝原因和网络状态。不要在不允许联网的环境中运行真实更新。

## 分类筛选返回 400

使用页面 select 提交，不把中文显示文本写进 URL。合法 category 是内部稳定值；清除筛选可恢复默认。若手工构造 URL，检查重复参数、非法日期、五位数年份和未知字段。

## AI Key 未配置

默认规则模式不需要 Key。AI 页面提示未配置时，填写兼容 provider 的 Base URL、模型和 Key，并先测试连接。不要把 Key 写进仓库、命令历史、截图或 Issue。数据库中保存的 Key 未加密。

## 端口被占用

macOS/Linux：

```bash
AIM_PORT=8765 ./scripts/run.sh
```

Windows：

```powershell
$env:AIM_PORT = "8765"
./scripts/run.ps1
```

## Windows 路径和编码

使用 PowerShell 7 或 Windows PowerShell，仓库路径避免受控目录和过长层级。保持文件为 UTF-8，不用系统记事本转换 YAML 编码。SQLite URL 使用正斜杠。

## 安全重建测试数据库

只对明确的测试路径操作，绝不使用 `data/intelligence.db`：

```bash
test_db="$(mktemp -d)/verify.db"
AIM_DATABASE_URL="sqlite:///$test_db" uv run --frozen alembic upgrade head
AIM_DATABASE_URL="sqlite:///$test_db" \
  uv run --frozen python -m app.cli sources sync-catalog --reconcile
```

更简单的方法是运行 `./scripts/verify.sh`。Windows 使用 `verify.ps1`，脚本会创建 GUID 临时目录并在结束时清理。

## 数据库锁定

停止重复 Web/CLI 进程，等待当前事务结束。SQLite 适合单机单实例；不要让多个调度器同时写同一个文件。强制终止前先确认没有导出、migration 或更新正在进行。
