# 第三方依赖许可证审查

本文件记录 `pyproject.toml` 中直接依赖在本次公开快照准备时的许可证元数据。它不是依赖包许可证正文，也不能替代发布者在正式发布前对锁文件中全部传递依赖进行法律审查。

| 许可证 | 直接依赖 |
| --- | --- |
| MIT | Alembic、Beautiful Soup、FastAPI、openpyxl、pydantic-settings、python-docx、PyYAML、SQLAlchemy、tzlocal、pytest、Ruff、Pyright |
| BSD-3-Clause | dateparser、HTTPX、lxml、Uvicorn |
| BSD-2-Clause | feedparser |
| Apache-2.0 | python-multipart、Tenacity、pytest-asyncio、types-PyYAML |
| 上游元数据需复核 | Jinja2（安装元数据未提供标准 `License` 值，分类器标记为 BSD） |

正式公开前应完成：

1. 确定本项目代码的发布许可证与著作权主体；
2. 用锁文件生成完整的直接及传递依赖清单；
3. 保存所有适用的许可证与 NOTICE 文本；
4. 对 UI 素材、截图、字体和图标的来源及授权单独确认；
5. 在升级依赖后重新执行许可证审查。

当前仓库的 `LICENSE` 是临时的“保留所有权利”声明，不授予开源使用权。
