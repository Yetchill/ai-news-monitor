"""Local-only FastAPI application factory."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.exc import SQLAlchemyError

from app.classifiers.manual import ManualCategoryError
from app.config import Settings, get_settings
from app.config.settings import PROJECT_ROOT
from app.domain.collection import Fetcher
from app.domain.enums import (
    Category,
    CrawlMode,
    CrawlStatus,
    DiscoveryStatus,
    PrimaryType,
    ReviewPolicy,
    ReviewStatus,
    RunTrigger,
    SourceKind,
    SourceRole,
    SourceType,
    VerificationStatus,
    Weekday,
)
from app.domain.exports import ExportError, ExportGenerationError
from app.domain.scheduling import SchedulerStatus
from app.exporters.common import PRIMARY_TYPE_LABELS
from app.services.application_factory import update_pipeline_context
from app.services.error_sanitization import sanitize_error
from app.services.schedule_settings_service import ScheduleValidationError
from app.services.scheduler_service import SchedulerClock, SchedulerReloadError
from app.services.source_discovery import DiscoveryTokenError, DiscoveryTokenStore
from app.services.source_lifecycle_service import SourceActivationError
from app.services.source_management import (
    ManagedSourceNotFoundError,
    SourceAlreadyExistsError,
    SourceManagementError,
)
from app.services.source_url_security import SourceUrlGuard, SourceUrlSecurityError
from app.services.update_pipeline import (
    SourceCandidateError,
    SourceDisabledError,
    SourceNotFoundError,
)
from app.services.web_data_service import EntityNotFoundError, SourceStateError
from app.storage.database import Database
from app.utils.logging import configure_logging
from app.web.dependencies import PipelineContextFactory, UpdateInProgressError, WebServices
from app.web.routes import router
from app.web.schemas import WebInputError

WEB_ROOT = Path(__file__).resolve().parent
CATEGORY_LABELS = {
    Category.MODEL_TECHNOLOGY: "模型与技术动态",
    Category.AGENT_PRODUCT: "智能体与产品更新",
    Category.ENTERPRISE_CASE: "企业成果与应用案例",
    Category.AWARD_CASE: "获奖与优秀案例",
    Category.SOLICITATION: "奖项与成果征集",
    Category.POLICY_INDUSTRY: "政策、标准与行业动态",
    Category.UNCLASSIFIED: "待分类",
}
VERIFICATION_LABELS = {
    VerificationStatus.OFFICIAL_CONFIRMED: "官方确认",
    VerificationStatus.OFFICIAL_LINKED: "官方链接",
    VerificationStatus.MULTI_SOURCE_CONFIRMED: "多源确认",
    VerificationStatus.MEDIA_ONLY: "仅媒体报道",
    VerificationStatus.RUMOR_OR_PREDICTION: "传闻或预测",
}
REVIEW_LABELS = {
    ReviewStatus.NOT_REQUIRED: "无需审核",
    ReviewStatus.PENDING: "待审核",
    ReviewStatus.APPROVED: "已通过",
    ReviewStatus.REJECTED: "已拒绝",
}
CLASSIFIER_PROVIDER_LABELS = {
    "rule_based": "规则分类",
    "llm": "AI 分类",
    "hybrid": "混合分类",
    "manual": "人工分类",
    "source_default": "来源默认",
}
SOURCE_KIND_LABELS = {
    SourceKind.FORMAL: "正式",
    SourceKind.TEST: "测试",
    SourceKind.FALLBACK: "备用",
}
SOURCE_ROLE_LABELS = {
    SourceRole.OFFICIAL_PRODUCT: "企业官方",
    SourceRole.OFFICIAL_POLICY: "政策官方",
    SourceRole.OFFICIAL_INDUSTRY: "行业机构",
    SourceRole.OPPORTUNITY_AND_AWARD_HUB: "申报与奖项平台",
    SourceRole.OFFICIAL_CASE_HUB: "官方案例库",
    SourceRole.REPORT_HUB: "报告平台",
    SourceRole.MEDIA_DISCOVERY: "媒体发现",
    SourceRole.FALLBACK: "备用来源",
}
CRAWL_MODE_LABELS = {
    CrawlMode.RSS: "RSS / Atom",
    CrawlMode.HTML_LIST: "网页列表",
    CrawlMode.SINGLE_PAGE_CHANGELOG: "更新日志",
    CrawlMode.DOCUMENT_HUB: "文档中心",
    CrawlMode.CASE_HUB: "案例中心",
    CrawlMode.API: "数据接口",
    CrawlMode.CUSTOM: "专用采集器",
    CrawlMode.RSSHUB: "RSSHub",
}
REVIEW_POLICY_LABELS = {
    ReviewPolicy.AUTO_PUBLISH: "自动发布",
    ReviewPolicy.REVIEW_ON_LOW_CONFIDENCE: "低置信度时审核",
    ReviewPolicy.ALWAYS_REVIEW: "始终审核",
    ReviewPolicy.NEVER_PUBLISH: "仅作线索",
}
STATUS_LABELS = {
    CrawlStatus.RUNNING: "运行中",
    CrawlStatus.SUCCESS: "成功",
    CrawlStatus.PARTIAL_SUCCESS: "部分成功",
    CrawlStatus.FAILED: "失败",
}
DISCOVERY_STATUS_LABELS = {
    DiscoveryStatus.READY: "可以使用",
    DiscoveryStatus.PARTIAL: "基本可用, 建议检查",
    DiscoveryStatus.NEEDS_CONFIGURATION: "需要配置",
    DiscoveryStatus.NEEDS_CUSTOM_COLLECTOR: "需要自定义采集器",
    DiscoveryStatus.BLOCKED: "网站拒绝访问",
    DiscoveryStatus.UNREACHABLE: "暂时无法访问",
}
SOURCE_TYPE_LABELS = {
    SourceType.RSS: "RSS / Atom",
    SourceType.HTML_LIST: "普通网页列表",
    SourceType.GITHUB_RELEASE: "GitHub Releases",
    SourceType.JSON_API: "JSON 接口",
    SourceType.CUSTOM: "自定义采集器",
}
TRIGGER_LABELS = {
    RunTrigger.LEGACY_MANUAL: "历史手动",
    RunTrigger.MANUAL_WEB: "网页手动",
    RunTrigger.MANUAL_CLI: "CLI 手动",
    RunTrigger.SCHEDULED: "定时",
}
WEEKDAY_LABELS = dict(
    zip(tuple(Weekday), ("周一", "周二", "周三", "周四", "周五", "周六", "周日"), strict=True)
)
SCHEDULER_STATUS_LABELS = {
    SchedulerStatus.DISABLED: "已关闭",
    SchedulerStatus.WAITING: "等待下一次运行",
    SchedulerStatus.RUNNING: "正在执行定时更新",
    SchedulerStatus.STOPPED: "未运行",
}
PROCESS_REASON_LABELS = {
    "source.configuration_invalid": "来源配置无效",
    "fetch.failed": "网络抓取失败",
    "parse_or_collection.failed": "页面解析或采集器失败",
    "normalization.failed": "内容规范化失败",
    "classification.failed": "分类失败 / 已按待分类保留",
    "persistence.failed": "数据库写入失败",
    "quality.below_minimum": "质量分低于来源门槛",
    "content.external_link_not_allowed": "来源不允许外部链接",
    "source.include_term_missing": "未命中来源准入关键词",
    "source.content_scope_mismatch": "内容范围不匹配",
}


def create_app(
    *,
    settings: Settings | None = None,
    database: Database | None = None,
    enforce_migrations: bool = True,
    pipeline_context_factory: PipelineContextFactory | None = None,
    source_fetcher: Fetcher | None = None,
    source_url_guard: SourceUrlGuard | None = None,
    token_store: DiscoveryTokenStore | None = None,
    scheduler_clock: SchedulerClock | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    resolved_database = database or Database.from_settings(resolved_settings)
    owns_database = database is None

    services = WebServices.build(
        resolved_database,
        pipeline_context_factory=pipeline_context_factory or update_pipeline_context,
        source_fetcher=source_fetcher,
        source_url_guard=source_url_guard,
        token_store=token_store,
        scheduler_clock=scheduler_clock,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        try:
            configure_logging(resolved_settings)
            if enforce_migrations:
                require_current_migration(resolved_database)
            await services.scheduler.start()
            yield
        finally:
            try:
                await services.aclose()
            finally:
                if owns_database:
                    resolved_database.dispose()

    application = FastAPI(
        title=resolved_settings.app_name,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    templates = _templates()
    application.state.templates = templates
    application.state.services = services
    application.mount("/static", StaticFiles(directory=WEB_ROOT / "static"), name="static")
    application.include_router(router)
    _register_error_handlers(application, templates)
    return application


def require_current_migration(database: Database) -> None:
    """Refuse startup when the configured database is not at Alembic head."""

    script = ScriptDirectory.from_config(_alembic_config())
    expected = script.get_current_head()
    with database.engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
    if current != expected:
        raise RuntimeError(
            "数据库结构未升级。请先运行 `uv run alembic upgrade head`, "
            f"当前版本为 {current or '未初始化'}, 目标版本为 {expected}。"
        )


def _alembic_config():  # type: ignore[no-untyped-def]
    from alembic.config import Config

    return Config(PROJECT_ROOT / "alembic.ini")


def _templates() -> Jinja2Templates:
    environment = Environment(
        loader=FileSystemLoader(WEB_ROOT / "templates"),
        autoescape=select_autoescape(("html", "xml"), default_for_string=True),
    )
    globals_mapping = cast(dict[str, object], environment.globals)
    globals_mapping["category_label"] = _category_label
    globals_mapping["primary_type_label"] = _primary_type_label
    globals_mapping["status_label"] = _status_label
    globals_mapping["format_time"] = _format_time
    globals_mapping["discovery_status_label"] = _discovery_status_label
    globals_mapping["discovery_type_label"] = _discovery_type_label
    globals_mapping["trigger_label"] = _trigger_label
    globals_mapping["weekday_label"] = _weekday_label
    globals_mapping["scheduler_status_label"] = _scheduler_status_label
    globals_mapping["process_reason_label"] = _process_reason_label
    globals_mapping["verification_label"] = _verification_label
    globals_mapping["review_label"] = _review_label
    globals_mapping["provider_label"] = _provider_label
    globals_mapping["source_kind_label"] = _source_kind_label
    globals_mapping["source_role_label"] = _source_role_label
    globals_mapping["crawl_mode_label"] = _crawl_mode_label
    globals_mapping["review_policy_label"] = _review_policy_label
    return Jinja2Templates(env=environment)


def _format_time(value: object) -> str:
    from datetime import datetime

    return value.strftime("%Y-%m-%d %H:%M") if isinstance(value, datetime) else "—"


def _category_label(value: Category | str) -> str:
    return CATEGORY_LABELS.get(Category(value), "待分类")


def _primary_type_label(value: PrimaryType | str) -> str:
    try:
        return PRIMARY_TYPE_LABELS.get(PrimaryType(value), "待确认")
    except ValueError:
        return "待确认"


def _status_label(value: CrawlStatus | str) -> str:
    return STATUS_LABELS.get(CrawlStatus(value), "未知")


def _process_reason_label(value: str) -> str:
    return PROCESS_REASON_LABELS.get(value, value)


def _discovery_status_label(value: DiscoveryStatus | str | None) -> str:
    if value is None:
        return "未检测"
    try:
        return DISCOVERY_STATUS_LABELS.get(DiscoveryStatus(value), "未知")
    except ValueError:
        return "未知"


def _discovery_type_label(value: SourceType | str) -> str:
    try:
        return SOURCE_TYPE_LABELS.get(SourceType(value), "未知")
    except ValueError:
        return "未知"


def _trigger_label(value: RunTrigger | str) -> str:
    try:
        return TRIGGER_LABELS.get(RunTrigger(value), "未知")
    except ValueError:
        return "未知"


def _weekday_label(value: Weekday | str) -> str:
    try:
        return WEEKDAY_LABELS.get(Weekday(value), "未知")
    except ValueError:
        return "未知"


def _scheduler_status_label(value: SchedulerStatus | str) -> str:
    try:
        return SCHEDULER_STATUS_LABELS.get(SchedulerStatus(value), "未知")
    except ValueError:
        return "未知"


def _verification_label(value: VerificationStatus | str) -> str:
    try:
        return VERIFICATION_LABELS.get(VerificationStatus(value), "未知")
    except ValueError:
        return "未知"


def _review_label(value: ReviewStatus | str) -> str:
    try:
        return REVIEW_LABELS.get(ReviewStatus(value), "未知")
    except ValueError:
        return "未知"


def _provider_label(value: str | None) -> str:
    if not value:
        return "未知"
    return CLASSIFIER_PROVIDER_LABELS.get(value, value)


def _source_kind_label(value: SourceKind | str) -> str:
    try:
        return SOURCE_KIND_LABELS.get(SourceKind(value), "未知")
    except ValueError:
        return "未知"


def _source_role_label(value: SourceRole | str) -> str:
    try:
        return SOURCE_ROLE_LABELS.get(SourceRole(value), "未知")
    except ValueError:
        return "未知"


def _crawl_mode_label(value: CrawlMode | str) -> str:
    try:
        return CRAWL_MODE_LABELS.get(CrawlMode(value), "未知")
    except ValueError:
        return "未知"


def _review_policy_label(value: ReviewPolicy | str) -> str:
    try:
        return REVIEW_POLICY_LABELS.get(ReviewPolicy(value), "未知")
    except ValueError:
        return "未知"


def _register_error_handlers(application: FastAPI, templates: Jinja2Templates) -> None:
    async def render_error(request: Request, status_code: int, message: str) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"status_code": status_code, "message": message},
            status_code=status_code,
        )

    async def input_error(request: Request, exc: Exception) -> HTMLResponse:
        return await render_error(request, 400, sanitize_error(exc, limit=300))

    async def request_validation_error(request: Request, _exc: Exception) -> HTMLResponse:
        return await render_error(request, 400, "提交参数无效, 请返回页面检查后重试。")

    async def not_found(request: Request, exc: Exception) -> HTMLResponse:
        return await render_error(request, 404, sanitize_error(exc, limit=200))

    async def update_conflict(request: Request, exc: Exception) -> HTMLResponse:
        return await render_error(request, 409, sanitize_error(exc, limit=200))

    async def database_error(request: Request, _exc: Exception) -> HTMLResponse:
        return await render_error(request, 500, "本地数据库暂时不可用, 请稍后重试并检查日志。")

    async def scheduler_reload_error(request: Request, exc: Exception) -> HTMLResponse:
        return await render_error(request, 503, sanitize_error(exc, limit=200))

    async def unexpected_error(request: Request, _exc: Exception) -> HTMLResponse:
        return await render_error(request, 500, "操作未能完成, 请稍后重试并检查应用日志。")

    application.add_exception_handler(WebInputError, input_error)
    application.add_exception_handler(SourceUrlSecurityError, input_error)
    application.add_exception_handler(SourceManagementError, input_error)
    application.add_exception_handler(SourceStateError, input_error)
    application.add_exception_handler(SourceActivationError, input_error)
    application.add_exception_handler(DiscoveryTokenError, input_error)
    application.add_exception_handler(ManualCategoryError, input_error)
    application.add_exception_handler(ExportError, input_error)
    application.add_exception_handler(ExportGenerationError, unexpected_error)
    application.add_exception_handler(SourceDisabledError, input_error)
    application.add_exception_handler(SourceCandidateError, input_error)
    application.add_exception_handler(ScheduleValidationError, input_error)
    application.add_exception_handler(SchedulerReloadError, scheduler_reload_error)
    application.add_exception_handler(RequestValidationError, request_validation_error)
    application.add_exception_handler(EntityNotFoundError, not_found)
    application.add_exception_handler(ManagedSourceNotFoundError, not_found)
    application.add_exception_handler(SourceAlreadyExistsError, update_conflict)
    application.add_exception_handler(SourceNotFoundError, not_found)
    application.add_exception_handler(UpdateInProgressError, update_conflict)
    application.add_exception_handler(SQLAlchemyError, database_error)
    application.add_exception_handler(Exception, unexpected_error)


app = create_app()
