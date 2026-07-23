# Changelog

本项目遵循语义化版本；日期使用 `YYYY-MM-DD`。

## [Unreleased]

- 尚未创建公开 GitHub 仓库。
- 发布前需要由版权所有者选择正式开源许可证。

## [0.1.0] - 2026-07-23

### Added

- 本地 FastAPI/Jinja2 资讯、AI、来源、设置和更新记录页面；
- SQLite/SQLAlchemy/Alembic 数据层及运行、来源、资讯、AI 任务审计；
- 26 条 canonical 来源目录，显式 reconcile 后为 18 active / 8 candidate；
- RSS、HTML、JSON、Release、文档/案例中心与单页更新日志采集接口；
- 内容准入、规范化、去重、taxonomy v2、规则/LLM/Hybrid 分类与人工覆盖；
- 已读、收藏、组合筛选、分页、来源详情、候选预览/激活；
- Excel/Word 导出、定时更新、错误净化和来源级运行明细；
- macOS/Linux 与 Windows bootstrap、run、verify 脚本；
- 用户、架构、复现、安全与故障排查文档。

### Fixed

- 修复分类筛选快捷时间参数导致的 400；
- 恢复真实原文链接及新标签安全属性；
- 重构更新完成页和更新记录为中文结构化展示；
- 修复 canonical catalog 与历史 preset 并存问题；
- 修复已读/未读条件未进入 Repository SQL；
- 修复更新记录日期与每页控件在宽屏重叠。

### Security

- 来源发现使用 URL/IP 校验、重定向复查、响应大小和超时限制；
- 页面默认仅监听 `127.0.0.1`；
- 默认规则模式不需要 API Key，不执行真实 AI 请求；
- 数据库、日志、导出、环境文件和开发工具配置均从公开快照排除。
