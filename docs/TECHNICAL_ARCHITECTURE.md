# 技术架构

## 总体架构

系统采用本地单体分层结构：FastAPI/Jinja2 Web 与 CLI 调用应用服务；应用服务编排 Fetcher、Collector、准入、分类、持久化和导出；Repository/Unit of Work 隔离 SQLAlchemy Session；SQLite 由 Alembic 管理结构。

```text
Web / CLI / Scheduler
        │
Application services
        │
Fetcher → Collector → Normalize → Admission → Taxonomy/Classify → Persist
        │                                                    │
   public HTTP                                      SQLAlchemy / SQLite
```

## 模块职责

- `app/domain/`：枚举、领域模型、查询值、采集/更新/导出协议，不依赖 Web。
- `app/fetchers/`：HTTP 请求、超时、并发、重试和错误分类。
- `app/collectors/`：RSS、HTML、JSON、Release 和专用来源解析；registry 按名称构造。
- `app/classifiers/`：规则、LLM、Hybrid 与人工分类接口。
- `app/services/`：更新流水线、准入、分类、来源生命周期、AI、调度、导出和 Web 查询。
- `app/storage/`：SQLAlchemy 模型仓储、事务边界和 Alembic migration。
- `app/web/`：严格参数 schema、POST 操作路由、Jinja2 模板和静态交互。
- `app/config/`：分类规则与唯一 canonical `source_catalog.yaml`。

## 更新数据流

1. UpdateExecutionService 获取进程内更新锁并创建 CrawlRun。
2. UpdatePipeline 按 enabled/active/formal 语义选择来源。
3. Fetcher 访问目标，Collector 生成 `CollectedItem`。
4. Normalization 清洗标题、URL、时间、简介和扩展字段。
5. ContentAdmissionPolicy 根据来源角色、include/exclude、质量和内容类型判定接受或拒绝。
6. Taxonomy 分类得到 primary type、主题/行业标签、可信与审核字段。
7. Rule/LLM/Hybrid 分类提供兼容业务分类；人工值始终优先。
8. Persistence 以 canonical URL 和 source-scoped fingerprint 去重，保存资讯、修订和来源发现。
9. CrawlRun 与 CrawlSourceExecution 记录发现、准入、拒绝、新增、更新、重复、分类和失败原因。

来源失败在来源级隔离；部分来源成功时运行状态为 partial success。失败信息在进入页面前净化。

## Fetcher、Collector 与安全边界

通用 Fetcher 基于 httpx，限制全局/同域并发、请求间隔、超时、重试和响应大小。来源发现使用独立安全 Fetcher：仅 HTTP(S)、限制端口、校验 DNS 和实际连接 IP、阻止私网/本地/元数据地址、逐跳验证重定向并忽略环境代理。

Collector 只负责把来源响应解析成统一对象，不负责业务分类或数据库事务。新增 Collector 必须注册名称、提供固定离线 fixture，并证明边界与失败行为。

## Admission、Taxonomy 与分类

准入和分类是两步：准入回答“内容是否值得进入当前来源业务范围”，分类回答“内容属于什么”。拒绝不是处理失败，二者分别统计。

- Rule：纯本地、确定性、默认模式；规则来自 YAML。
- LLM：将内容发送到配置的 OpenAI-compatible provider，失败不会伪装成功。
- Hybrid：规则与 LLM 协作，并保留 fallback 和 provider 记录。
- Manual：人工分类/primary type 覆盖自动结果，后续更新不得清除。

## Source catalog 与生命周期

`app/config/source_catalog.yaml` 是唯一 canonical 真值，当前 version 1，共 26 条。`sources` 表是运行时投影并保存状态、诊断和历史关联。

```bash
python -m app.cli sources sync-catalog --reconcile
```

reconcile 以 slug 对齐：active 进入监控，candidate 保持禁用；无引用旧 preset 可删除，有资讯或运行引用则 paused/fallback 保留历史。普通 GET 不同步。用户新增来源不属于 legacy preset，仍走 candidate 预览/激活。

## AI 设置与任务

`ai_settings` 是单例运行配置，`ai_jobs` 记录任务类型、触发方式、成功/失败/跳过/fallback 和净化错误。Key 页面显示掩码，但数据库字段不是加密保险库。默认 classifier 为 rule，AI Web 配置默认为关闭。

## Web 层

GET 路由只读；收藏、已读、分类、来源状态、设置和更新均使用 POST。Pydantic schema 严格验证查询参数，合法 option 使用内部稳定枚举值，非法外部输入返回清晰 400。列表分页、导出和两种视图共享同一 `ItemQuery`/Repository 条件。

服务默认绑定 `127.0.0.1`。当前没有账户、权限或 CSRF，不适合直接公网部署。

## 数据库与 migration

主要表：

- `sources`：来源配置、生命周期、catalog 元数据和最近状态；
- `intelligence_items`：原文、分类、审核、已读、收藏、AI 总结；
- `item_revisions`、`item_review_events`：内容变化和人工审核审计；
- `crawl_runs`、`crawl_source_executions`：运行及来源级统计；
- `schedule_settings`：单例调度配置；
- `ai_settings`、`ai_jobs`：AI 配置和任务记录。

从零必须 `alembic upgrade head`。migration 不假定来源已初始化，也不应为修复目录而改写历史 migration。

## 错误回退

- 采集器错误映射为结构化失败并隔离来源；
- LLM/Hybrid 可按策略回退规则结果，任务记录 fallback；
- 无法安全解析或低质量内容拒绝，不写成成功资讯；
- 写入使用事务和保存点处理唯一约束竞争；
- 调度、Web 和 CLI 的同进程更新共享锁，但多个独立进程间没有分布式锁。

## 测试体系

- 单元测试使用临时 SQLite 和固定 HTML/XML/JSON fixture；
- Web 测试通过 TestClient 验证模板、严格参数和 POST 持久化；
- migration 测试覆盖从旧 revision 升级和历史数据保护；
- `network` marker 测试真实公开来源，默认排除；
- Ruff 和 strict Pyright 覆盖 `app/`、`tests/`。

## 扩展新来源

1. 优先复用 RSS/HTML/JSON/Release Collector；
2. 在隔离 fixture 上定义解析、分页、日期、URL 和上限；
3. 注册 Collector，补单元测试；
4. 在 canonical YAML 增加稳定 slug 和完整安全/准入配置；
5. 先设 candidate，执行无业务落库 preview；
6. 验证链接、日期、重复率和质量后显式 activate；
7. 不在 GET 页面隐式 reconcile，不把示例数据写入生产模板。

## 已知限制

- 本地单用户应用，无认证、授权和 CSRF；
- SQLite 和进程内锁不适合多实例并发；
- 调度仅在 Web 进程存活时运行，不补跑关机窗口；
- 部分来源受 JavaScript/WAF 影响，保持 candidate 或需要专用 Collector；
- AI Key 本地存储未使用系统密钥链或字段加密；
- 浏览器自动化、系统托盘、桌面安装包、邮件推送尚未提供。
