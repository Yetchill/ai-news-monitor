"""Server-rendered HTML pages and POST-only manual operations."""

import re
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import quote, urlencode, urlsplit
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, Path, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.domain.enums import (
    Category,
    CrawlStatus,
    PrimaryType,
    ReviewStatus,
    RunTrigger,
    SourceScope,
    VerificationStatus,
    Weekday,
)
from app.domain.exports import ExportFormat, ExportQuery
from app.domain.onboarding import DiscoverySession
from app.domain.update import UpdateResult
from app.services.error_sanitization import sanitize_error
from app.services.schedule_settings_service import (
    ScheduleValidationError,
    parse_initial_fetch_days,
    parse_time,
    validate_timezone,
)
from app.services.scheduler_service import SchedulerReloadError
from app.services.source_lifecycle_service import SourceActivationError
from app.web.schemas import (
    MAX_DATABASE_ID,
    ItemQueryParams,
    RunQueryParams,
    SourceQueryParams,
    WebInputError,
    local_day_bounds,
)

router = APIRouter()
_ASCII_DOWNLOAD_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


@router.get("/leadership", response_class=HTMLResponse)
async def leadership_redirect(request: Request) -> RedirectResponse:
    return RedirectResponse("/", status_code=301)


@router.get("/", response_class=HTMLResponse)
async def items_page(request: Request) -> HTMLResponse:
    params = ItemQueryParams.parse(dict(request.query_params))
    services = request.app.state.services
    now = datetime.now(UTC)
    today_start, today_end = local_day_bounds(now)
    page = services.data.list_items(params.to_domain(now=now))
    _require_existing_page(page.page, page.total_pages)
    return request.app.state.templates.TemplateResponse(
        request,
        "items.html",
        {
            "page": page,
            "filters": params,
            "source_options": services.data.source_options(),
            "categories": tuple(Category),
            "primary_types": tuple(PrimaryType),
            "verification_statuses": tuple(VerificationStatus),
            "review_statuses": tuple(ReviewStatus),
            "previous_url": _page_url("/", params.query_values(), page.page - 1)
            if page.page > 1
            else None,
            "next_url": _page_url("/", params.query_values(), page.page + 1)
            if page.page < page.total_pages
            else None,
            "return_to": _current_path(request),
            "export_values": params.export_values(),
            "overview": services.data.item_overview(
                today_start=today_start,
                today_end=today_end,
                source_scope=SourceScope(params.source_scope),
            ),
            "today_date": today_start.astimezone(ZoneInfo("Asia/Shanghai")).date(),
        },
    )


@router.post("/exports/{export_format}")
async def export_items(request: Request, export_format: ExportFormat) -> Response:
    form = await request.form()
    values: dict[str, str] = {}
    for key, value in form.multi_items():
        if not isinstance(value, str) or key in values:
            raise WebInputError("导出筛选参数无效, 请返回资讯页后重试。")
        values[key] = value
    params = ItemQueryParams.parse(values)
    result = request.app.state.services.exports.export(
        export_format,
        ExportQuery(filters=params.to_filter(for_export=True)),
    )
    return Response(
        content=result.content,
        media_type=result.media_type,
        headers={
            "Content-Disposition": _content_disposition(result.filename, result.ascii_filename),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/sources", response_class=HTMLResponse)
async def sources_page(request: Request) -> HTMLResponse:
    values = {
        key: value
        for key, value in request.query_params.items()
        if key in {"page", "per_page", "search", "filter", "tab"}
    }
    if values.get("filter") == "candidate" and "tab" not in values:
        values["tab"] = "candidate"
    params = SourceQueryParams.parse(values)
    page = request.app.state.services.data.list_sources(
        page=params.page,
        per_page=params.per_page,
        catalog_filter=params.effective_filter(),
        search=params.search,
    )
    _require_existing_page(page.page, page.total_pages)
    page_values = params.query_values()
    return request.app.state.templates.TemplateResponse(
        request,
        "sources.html",
        {
            "page": page,
            "previous_url": _page_url("/sources", page_values, page.page - 1)
            if page.page > 1
            else None,
            "next_url": _page_url("/sources", page_values, page.page + 1)
            if page.page < page.total_pages
            else None,
            "return_to": _current_path(request),
            "formal_source_count": request.app.state.services.data.formal_source_count(),
            "seeded": request.query_params.get("seeded") == "1",
            "seed_created": request.query_params.get("created"),
            "seed_promoted": request.query_params.get("promoted"),
            "seed_conflicts": request.query_params.get("conflicts"),
            "catalog_filter": params.filter,
            "filters": params,
            "source_overview": request.app.state.services.data.source_overview(),
        },
    )


@router.post("/sources/seed-formal", response_class=HTMLResponse)
async def seed_formal_sources(request: Request) -> RedirectResponse:
    result = request.app.state.services.source_seed.seed(reconcile=True)
    query = urlencode(
        {
            "seeded": "1",
            "created": str(result.created),
            "promoted": str(result.promoted),
            "conflicts": str(result.conflicts),
        }
    )
    return RedirectResponse(f"/sources?{query}", status_code=303)


@router.get("/sources/new", response_class=HTMLResponse)
async def new_source_page(request: Request) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(request, "source-new.html", {})


@router.post("/sources/discover", response_class=HTMLResponse)
async def discover_source(
    request: Request,
    url: Annotated[str, Form(min_length=1, max_length=2048)],
) -> RedirectResponse:
    token = await request.app.state.services.onboarding.start(url)
    return RedirectResponse(f"/sources/discover/{token}", status_code=303)


@router.get("/sources/discover/{token}", response_class=HTMLResponse)
async def discovery_page(
    request: Request,
    token: Annotated[str, Path(min_length=1, max_length=128)],
) -> HTMLResponse:
    session: DiscoverySession = request.app.state.services.sources.get_discovery(token)
    return request.app.state.templates.TemplateResponse(
        request,
        "source-discovery.html",
        {
            "token": token,
            "session": session,
            "categories": tuple(Category),
            "suggested_name": str(urlsplit(session.discovery.normalized_url).hostname or "新来源"),
        },
    )


@router.post("/sources", response_class=HTMLResponse)
async def create_source(
    request: Request,
    token: Annotated[str, Form(min_length=1, max_length=128)],
    name: Annotated[str, Form(min_length=1, max_length=255)],
    default_category: Annotated[str, Form(max_length=50)] = "",
    description: Annotated[str, Form(max_length=2000)] = "",
    enabled: Annotated[str | None, Form(max_length=5)] = None,
    action: Annotated[str, Form(max_length=30)] = "save",
) -> Response:
    if enabled not in {None, "true"}:
        raise WebInputError("来源状态无效。")
    if action not in {"save", "save_and_update"}:
        raise WebInputError("保存操作无效。")
    source = request.app.state.services.sources.create_from_token(
        token,
        name=name,
        default_category=default_category or None,
        enabled=enabled == "true",
        description=description or None,
    )
    if action == "save_and_update" and source.enabled:
        result = await request.app.state.services.updates.update(source_id=source.id)
        return _update_response(request, result)
    return RedirectResponse(f"/sources/{source.id}?saved=1", status_code=303)


@router.get("/sources/{source_id}", response_class=HTMLResponse)
async def source_detail_page(
    request: Request,
    source_id: Annotated[int, Path(ge=1, le=MAX_DATABASE_ID)],
) -> HTMLResponse:
    source = request.app.state.services.sources.get_source(source_id)
    data = request.app.state.services.data
    return request.app.state.templates.TemplateResponse(
        request,
        "source-detail.html",
        {
            "source": source,
            "categories": tuple(Category),
            "saved": request.query_params.get("saved") == "1",
            "updated": request.query_params.get("updated") == "1",
            "return_to": _current_path(request),
            "period_stats": data.source_period_stats(source_id),
            "recent_runs": data.source_recent_runs(source_id),
            "recent_items": data.source_recent_items(source_id),
        },
    )


@router.post("/sources/{source_id}/edit", response_class=HTMLResponse)
async def edit_source(
    request: Request,
    source_id: Annotated[int, Path(ge=1, le=MAX_DATABASE_ID)],
    name: Annotated[str, Form(min_length=1, max_length=255)],
    default_category: Annotated[str, Form(max_length=50)] = "",
    description: Annotated[str, Form(max_length=2000)] = "",
    enabled: Annotated[str | None, Form(max_length=5)] = None,
) -> RedirectResponse:
    if enabled not in {None, "true"}:
        raise WebInputError("来源状态无效。")
    request.app.state.services.sources.edit(
        source_id,
        name=name,
        default_category=default_category or None,
        enabled=enabled == "true",
        description=description or None,
    )
    return RedirectResponse(f"/sources/{source_id}?updated=1", status_code=303)


@router.post("/sources/{source_id}/rediscover", response_class=HTMLResponse)
async def rediscover_source(
    request: Request,
    source_id: Annotated[int, Path(ge=1, le=MAX_DATABASE_ID)],
) -> RedirectResponse:
    source = request.app.state.services.sources.get_source(source_id)
    if source.start_url is None:
        raise WebInputError("当前来源网址无效, 无法重新检测。")
    token = await request.app.state.services.onboarding.start(
        source.start_url, rediscover_source_id=source_id
    )
    return RedirectResponse(f"/sources/discover/{token}", status_code=303)


@router.post("/sources/{source_id}/rediscover/confirm", response_class=HTMLResponse)
async def confirm_rediscovery(
    request: Request,
    source_id: Annotated[int, Path(ge=1, le=MAX_DATABASE_ID)],
    token: Annotated[str, Form(min_length=1, max_length=128)],
) -> RedirectResponse:
    request.app.state.services.sources.confirm_rediscovery(source_id, token)
    return RedirectResponse(f"/sources/{source_id}?updated=1", status_code=303)


@router.get("/runs", response_class=HTMLResponse)
async def runs_page(request: Request) -> HTMLResponse:
    params = RunQueryParams.parse(dict(request.query_params))
    started_from, started_to = params.bounds()
    page = request.app.state.services.data.list_crawl_runs(
        page=params.page,
        per_page=params.per_page,
        status=params.status,
        trigger=params.trigger,
        started_from=started_from,
        started_to=started_to,
    )
    _require_existing_page(page.page, page.total_pages)
    values = params.query_values()
    today_start, _today_end = local_day_bounds()
    return request.app.state.templates.TemplateResponse(
        request,
        "runs.html",
        {
            "page": page,
            "previous_url": _page_url("/runs", values, page.page - 1) if page.page > 1 else None,
            "next_url": _page_url("/runs", values, page.page + 1)
            if page.page < page.total_pages
            else None,
            "filters": params,
            "statuses": tuple(CrawlStatus),
            "triggers": tuple(RunTrigger),
            "overview": request.app.state.services.data.crawl_run_overview(
                since=today_start - timedelta(days=6)
            ),
            "today_date": today_start.astimezone(ZoneInfo("Asia/Shanghai")).date(),
        },
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    view = request.app.state.services.scheduler.view()
    try:
        zone = validate_timezone(view.settings.timezone)
    except ScheduleValidationError:
        zone = ZoneInfo("UTC")
    return request.app.state.templates.TemplateResponse(
        request,
        "settings.html",
        {
            "view": view,
            "weekdays": tuple(Weekday),
            "saved": request.query_params.get("saved") == "1",
            "next_run_local": view.next_run_at.astimezone(zone) if view.next_run_at else None,
            "last_trigger_local": (
                view.settings.last_scheduled_trigger_at.astimezone(zone)
                if view.settings.last_scheduled_trigger_at
                else None
            ),
            "timezone_error": view.error,
            "display_timezone": view.settings.timezone if view.error is None else "UTC 回退显示",
        },
    )


@router.post("/settings", response_class=HTMLResponse)
async def save_settings(
    request: Request,
    schedule_time: Annotated[str, Form(min_length=5, max_length=5)],
    days: Annotated[list[str], Form()],
    timezone: Annotated[str, Form(min_length=1, max_length=100)],
    initial_fetch_days: Annotated[str, Form(min_length=1, max_length=3)],
    enabled: Annotated[str | None, Form(max_length=5)] = None,
) -> RedirectResponse:
    if enabled not in {None, "true"}:
        raise WebInputError("定时更新开关无效。")
    hour, minute = parse_time(schedule_time)
    request.app.state.services.schedule_settings.save(
        enabled=enabled == "true",
        hour=hour,
        minute=minute,
        days=days,
        timezone=timezone,
        initial_fetch_days=parse_initial_fetch_days(initial_fetch_days),
    )
    try:
        await request.app.state.services.scheduler.reload()
    except Exception as exc:
        raise SchedulerReloadError(
            "设置已保存, 但调度器未能立即重载; 请重试保存或重启应用。"
        ) from exc
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/items/{item_id}/favorite", response_class=HTMLResponse)
async def set_favorite(
    request: Request,
    item_id: Annotated[int, Path(ge=1, le=MAX_DATABASE_ID)],
    favorite: Annotated[str, Form(max_length=5)],
    return_to: Annotated[str, Form(max_length=2048)] = "/",
) -> RedirectResponse:
    if favorite not in {"true", "false"}:
        raise WebInputError("收藏状态无效。")
    request.app.state.services.data.set_favorite(item_id, favorite == "true")
    return RedirectResponse(_safe_return_to(return_to, default="/"), status_code=303)


@router.post("/items/{item_id}/read", response_class=HTMLResponse)
async def set_read(
    request: Request,
    item_id: Annotated[int, Path(ge=1, le=MAX_DATABASE_ID)],
    is_read: Annotated[str, Form(max_length=5)],
    return_to: Annotated[str, Form(max_length=2048)] = "/",
) -> RedirectResponse:
    if is_read not in {"true", "false"}:
        raise WebInputError("阅读状态无效。")
    request.app.state.services.data.set_read_status(item_id, is_read == "true")
    return RedirectResponse(_safe_return_to(return_to, default="/"), status_code=303)


@router.post("/items/batch-read", response_class=HTMLResponse)
async def batch_set_read(
    request: Request,
    item_ids: Annotated[str, Form(max_length=10000)],
    is_read: Annotated[str, Form(max_length=5)],
    return_to: Annotated[str, Form(max_length=2048)] = "/",
) -> RedirectResponse:
    if is_read not in {"true", "false"}:
        raise WebInputError("阅读状态无效。")
    ids = [int(sid) for sid in item_ids.split(",") if sid.strip().isdigit()]
    if not ids:
        raise WebInputError("未选择任何资讯。")
    request.app.state.services.data.batch_set_read_status(ids, is_read == "true")
    return RedirectResponse(_safe_return_to(return_to, default="/"), status_code=303)


@router.post("/items/{item_id}/category", response_class=HTMLResponse)
async def set_category(
    request: Request,
    item_id: Annotated[int, Path(ge=1, le=MAX_DATABASE_ID)],
    category: Annotated[str, Form(max_length=50)] = "",
    return_to: Annotated[str, Form(max_length=2048)] = "/",
) -> RedirectResponse:
    request.app.state.services.data.set_manual_category(item_id, category or None)
    return RedirectResponse(_safe_return_to(return_to, default="/"), status_code=303)


@router.post("/items/{item_id}/review", response_class=HTMLResponse)
async def set_item_review(
    request: Request,
    item_id: Annotated[int, Path(ge=1, le=MAX_DATABASE_ID)],
    primary_type: Annotated[str, Form(max_length=50)],
    verification_status: Annotated[str, Form(max_length=50)],
    review_status: Annotated[str, Form(max_length=50)],
    official_url: Annotated[str, Form(max_length=2048)] = "",
    return_to: Annotated[str, Form(max_length=2048)] = "/",
) -> RedirectResponse:
    request.app.state.services.data.set_taxonomy_review(
        item_id,
        primary_type=primary_type,
        verification_status=verification_status,
        review_status=review_status,
        official_url=official_url or None,
        actor_source="web_system_operator",
    )
    return RedirectResponse(_safe_return_to(return_to, default="/"), status_code=303)


@router.post("/sources/{source_id}/enabled", response_class=HTMLResponse)
async def set_source_enabled(
    request: Request,
    source_id: Annotated[int, Path(ge=1, le=MAX_DATABASE_ID)],
    enabled: Annotated[str, Form(max_length=5)],
    return_to: Annotated[str, Form(max_length=2048)] = "/sources",
) -> RedirectResponse:
    if enabled not in {"true", "false"}:
        raise WebInputError("来源状态无效。")
    request.app.state.services.data.set_source_enabled(source_id, enabled == "true")
    return RedirectResponse(_safe_return_to(return_to, default="/sources"), status_code=303)


@router.post("/updates", response_class=HTMLResponse)
async def update_all(request: Request) -> HTMLResponse:
    result = await request.app.state.services.updates.update()
    return _update_response(request, result)


@router.post("/sources/{source_id}/updates", response_class=HTMLResponse)
async def update_source(
    request: Request,
    source_id: Annotated[int, Path(ge=1, le=MAX_DATABASE_ID)],
) -> HTMLResponse:
    result = await request.app.state.services.updates.update(source_id=source_id)
    return _update_response(request, result)


@router.post("/sources/{source_id}/preview", response_class=HTMLResponse)
async def preview_source(
    request: Request,
    source_id: Annotated[int, Path(ge=1, le=MAX_DATABASE_ID)],
) -> HTMLResponse:
    result = await request.app.state.services.updates.preview(source_id)
    source = request.app.state.services.sources.get_source(source_id)
    if source.slug:
        request.app.state.services.source_lifecycle.record_preview(source.slug, result)
    return request.app.state.templates.TemplateResponse(
        request,
        "source-preview.html",
        {"result": result},
    )


@router.post("/sources/{source_id}/activate", response_class=HTMLResponse)
async def activate_source(
    request: Request,
    source_id: Annotated[int, Path(ge=1, le=MAX_DATABASE_ID)],
    confirm: Annotated[str, Form(max_length=5)],
) -> HTMLResponse:
    if confirm != "true":
        raise WebInputError("激活必须明确确认。")
    source = request.app.state.services.sources.get_source(source_id)
    if not source.slug:
        raise WebInputError("来源缺少稳定 slug, 不能激活。")
    result = await request.app.state.services.updates.preview(source_id)
    try:
        request.app.state.services.source_lifecycle.activate(source.slug, result, confirm=True)
    except SourceActivationError as exc:
        return request.app.state.templates.TemplateResponse(
            request,
            "source-preview.html",
            {
                "result": result,
                "activation_failed": True,
                "activation_error": sanitize_error(exc, limit=300),
            },
        )
    return request.app.state.templates.TemplateResponse(
        request,
        "source-preview.html",
        {"result": result, "activated": True},
    )


@router.get("/ai", response_class=HTMLResponse)
async def ai_page(request: Request) -> HTMLResponse:
    config = request.app.state.services.ai_settings.get_config()
    recent = request.app.state.services.ai_ops.get_recent_jobs(10)
    active_job = request.app.state.services.ai_ops.get_active_job()
    return request.app.state.templates.TemplateResponse(
        request,
        "ai.html",
        {
            "config": config,
            "recent_jobs": recent,
            "active_job_data": (
                request.app.state.services.ai_ops.job_data(active_job)
                if active_job is not None
                else None
            ),
            "provider_configs": (request.app.state.services.ai_settings.list_provider_configs()),
            "saved": request.query_params.get("saved") == "1",
            "key_cleared": request.query_params.get("key_cleared") == "1",
            "test_result": request.query_params.get("test_result"),
            "test_ok": request.query_params.get("test_ok") == "1",
            "work_overview": request.app.state.services.data.ai_work_overview(),
        },
    )


@router.post("/ai/save", response_class=HTMLResponse)
async def save_ai_settings(request: Request) -> RedirectResponse:
    from app.services.ai_settings_service import AIConfig

    form = await request.form()
    config = AIConfig(
        provider=_form_str(form, "provider", "deepseek"),
        base_url=_form_str(form, "base_url", "https://api.deepseek.com"),
        model=_form_str(form, "model", "deepseek-chat"),
        api_key=_form_str(form, "api_key", ""),
        timeout_seconds=_form_int(form, "timeout_seconds", 30),
        max_retries=_form_int(form, "max_retries", 1),
        enabled=_form_str(form, "enabled", "true") == "true",
        classifier_mode=_form_str(form, "classifier_mode", "off"),
        summarizer_mode=_form_str(form, "summarizer_mode", "off"),
        classification_provider=_form_str(form, "classification_provider", "deepseek"),
        classification_model=_form_str(form, "classification_model", "deepseek-chat"),
        summarization_provider=_form_str(form, "summarization_provider", "deepseek"),
        summarization_model=_form_str(form, "summarization_model", "deepseek-chat"),
        classification_batch_mode=_form_str(form, "classification_batch_mode", "smart"),
    )
    request.app.state.services.ai_settings.save(config)
    return RedirectResponse("/ai?saved=1", status_code=303)


@router.post("/ai/clear-key", response_class=HTMLResponse)
async def clear_ai_key(request: Request) -> RedirectResponse:
    form = await request.form()
    provider = _form_str(form, "provider", "deepseek")
    request.app.state.services.ai_settings.clear_key(provider)
    return RedirectResponse(f"/ai?key_cleared=1&provider={quote(provider)}", status_code=303)


@router.get("/ai/providers/{provider}")
async def get_ai_provider_config(request: Request, provider: str) -> JSONResponse:
    from app.services.ai_settings_service import PROVIDER_DEFAULTS

    if provider not in PROVIDER_DEFAULTS:
        raise WebInputError("不支持的 AI 供应商。")
    config = request.app.state.services.ai_settings.get_config(provider)
    return JSONResponse(config.safe_provider_data())


@router.post("/ai/test-connection", response_class=HTMLResponse)
async def test_ai_connection(request: Request) -> Response:
    from app.classifiers.providers import (
        LLMAuthenticationError,
        LLMConfigError,
        LLMModelError,
        LLMNetworkError,
        LLMProviderError,
        LLMResponseError,
        LLMTimeoutError,
        OpenAICompatibleProvider,
    )
    from app.services.ai_settings_service import provider_label

    form = await request.form()
    provider_name = _form_str(form, "provider", "deepseek")
    test_key = _form_str(form, "api_key", "")
    if not test_key:
        test_key = request.app.state.services.ai_settings.get_config(provider_name).api_key
    model = _form_str(form, "model", "")
    base_url = _form_str(form, "base_url", "")
    label = provider_label(provider_name)
    result_data: dict[str, object] = {
        "ok": False,
        "provider": provider_name,
        "provider_label": label,
        "model": model,
        "latency_ms": None,
    }
    if not test_key:
        result_data["message"] = "API Key 未配置"
        return _connection_test_response(request, result_data)
    provider = OpenAICompatibleProvider(
        base_url=base_url,
        api_key=test_key,
        model=model,
        timeout_seconds=min(60, max(5, _form_int(form, "timeout_seconds", 30))),
    )
    try:
        tested = await provider.test_connection()
        result_data.update(
            ok=True,
            latency_ms=tested.latency_ms,
            message="连接可用 (连接成功)",
        )
    except LLMAuthenticationError:
        result_data["message"] = "API Key 无效"
    except LLMModelError:
        result_data["message"] = "模型不存在或无权限"
    except LLMTimeoutError:
        result_data["message"] = "请求超时"
    except LLMNetworkError:
        result_data["message"] = "网络连接失败"
    except LLMResponseError:
        result_data["message"] = "返回格式异常"
    except LLMConfigError:
        result_data["message"] = "配置无效"
    except LLMProviderError as exc:
        safe_message = sanitize_error(exc, limit=100)
        result_data["message"] = safe_message.replace(test_key, "[REDACTED]")
    finally:
        await provider.aclose()
    return _connection_test_response(request, result_data)


@router.post("/ai/classify", response_class=HTMLResponse)
async def run_ai_classify(request: Request) -> RedirectResponse:
    form = await request.form()
    item_ids_str = _form_str(form, "item_ids", "")
    if item_ids_str:
        ids = _parse_ids(item_ids_str)
    else:
        ids = []

    mode = _form_str(form, "classification_batch_mode", "smart")
    reclassify = _form_str(form, "reclassify", "") == "true"
    task, created = request.app.state.services.ai_ops.enqueue_classification(
        ids or None,
        trigger="manual",
        mode=mode,
        reclassify=reclassify,
    )
    if created:
        request.app.state.services.background_tasks.start(
            request.app.state.services.ai_ops.run_classification_task(task),
            name=f"ai-classification-{task.job_id}",
        )
    return RedirectResponse(f"/ai?job_id={task.job_id}", status_code=303)


@router.post("/ai/summarize", response_class=HTMLResponse)
async def run_ai_summarize(request: Request) -> RedirectResponse:
    form = await request.form()
    item_ids_str = _form_str(form, "item_ids", "")
    retry = _form_str(form, "retry", "") == "1"
    ids = _parse_ids(item_ids_str) if item_ids_str else None
    task, created = request.app.state.services.ai_ops.enqueue_summarization(
        ids, trigger="manual", retry_failed_only=retry
    )
    if created:
        request.app.state.services.background_tasks.start(
            request.app.state.services.ai_ops.run_summarization_task(task),
            name=f"ai-summarization-{task.job_id}",
        )
    return RedirectResponse(f"/ai?job_id={task.job_id}", status_code=303)


@router.post("/items/{item_id}/ai-classify", response_class=HTMLResponse)
async def ai_classify_single(
    request: Request,
    item_id: Annotated[int, Path(ge=1, le=MAX_DATABASE_ID)],
    return_to: Annotated[str, Form(max_length=2048)] = "/",
) -> RedirectResponse:
    task, created = request.app.state.services.ai_ops.enqueue_classification(
        [item_id], trigger="manual", mode=None, reclassify=False
    )
    if created:
        request.app.state.services.background_tasks.start(
            request.app.state.services.ai_ops.run_classification_task(task),
            name=f"ai-classification-{task.job_id}",
        )
    return RedirectResponse(_safe_return_to(return_to, default="/"), status_code=303)


@router.post("/items/batch-ai-classify", response_class=HTMLResponse)
async def ai_classify_batch(
    request: Request,
    item_ids: Annotated[str, Form(max_length=10000)],
    return_to: Annotated[str, Form(max_length=2048)] = "/",
) -> RedirectResponse:
    ids = _parse_ids(item_ids)
    if not ids:
        raise WebInputError("未选择任何资讯。")
    task, created = request.app.state.services.ai_ops.enqueue_classification(
        ids, trigger="manual", mode=None, reclassify=False
    )
    if created:
        request.app.state.services.background_tasks.start(
            request.app.state.services.ai_ops.run_classification_task(task),
            name=f"ai-classification-{task.job_id}",
        )
    return RedirectResponse(_safe_return_to(return_to, default="/"), status_code=303)


@router.post("/items/{item_id}/ai-summarize", response_class=HTMLResponse)
async def ai_summarize_single(
    request: Request,
    item_id: Annotated[int, Path(ge=1, le=MAX_DATABASE_ID)],
    return_to: Annotated[str, Form(max_length=2048)] = "/",
) -> RedirectResponse:
    task, created = request.app.state.services.ai_ops.enqueue_summarization(
        [item_id], trigger="manual", retry_failed_only=False
    )
    if created:
        request.app.state.services.background_tasks.start(
            request.app.state.services.ai_ops.run_summarization_task(task),
            name=f"ai-summarization-{task.job_id}",
        )
    return RedirectResponse(_safe_return_to(return_to, default="/"), status_code=303)


@router.post("/items/batch-ai-summarize", response_class=HTMLResponse)
async def ai_summarize_batch(
    request: Request,
    item_ids: Annotated[str, Form(max_length=10000)],
    return_to: Annotated[str, Form(max_length=2048)] = "/",
) -> RedirectResponse:
    ids = _parse_ids(item_ids)
    if not ids:
        raise WebInputError("未选择任何资讯。")
    task, created = request.app.state.services.ai_ops.enqueue_summarization(
        ids, trigger="manual", retry_failed_only=False
    )
    if created:
        request.app.state.services.background_tasks.start(
            request.app.state.services.ai_ops.run_summarization_task(task),
            name=f"ai-summarization-{task.job_id}",
        )
    return RedirectResponse(_safe_return_to(return_to, default="/"), status_code=303)


@router.get("/ai/jobs/status")
async def ai_job_status(request: Request) -> JSONResponse:
    requested = request.query_params.get("job_id")
    if requested:
        try:
            job = request.app.state.services.ai_ops.get_job(int(requested))
        except (ValueError, TypeError):
            raise WebInputError("AI 任务编号无效。") from None
    else:
        job = request.app.state.services.ai_ops.get_active_job()
    data = request.app.state.services.ai_ops.job_data(job) if job is not None else None
    return JSONResponse(
        {
            "job": data,
            "work_overview": {
                "unclassified": request.app.state.services.data.ai_work_overview().unclassified,
                "unsummarized": request.app.state.services.data.ai_work_overview().unsummarized,
            },
        }
    )


def _connection_test_response(request: Request, data: dict[str, object]) -> Response:
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse(data, status_code=200)
    latency = f"\n延迟: {data['latency_ms']} 毫秒" if data.get("latency_ms") is not None else ""
    message = f"{data['message']}\n供应商: {data['provider_label']}\n模型: {data['model']}{latency}"
    query = urlencode(
        {
            "test_result": message,
            "test_ok": "1" if data.get("ok") else "0",
        }
    )
    return RedirectResponse(f"/ai?{query}", status_code=303)


def _update_response(request: Request, result: UpdateResult) -> HTMLResponse:
    error_summary = result.error_summary
    return request.app.state.templates.TemplateResponse(
        request,
        "update-result.html",
        {
            "result": result,
            "error_summary": sanitize_error(error_summary, limit=300) if error_summary else None,
            "sanitized_source_errors": {
                source.source_id: sanitize_error(source.error, limit=300)
                for source in result.source_results
                if source.error
            },
        },
    )


def _page_url(path: str, values: dict[str, str], page: int) -> str:
    query = {**values, "page": str(page)}
    return f"{path}?{urlencode(query)}"


def _current_path(request: Request) -> str:
    return request.url.path + (f"?{request.url.query}" if request.url.query else "")


def _safe_return_to(value: str, *, default: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme
        or parsed.netloc
        or not parsed.path.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or "\r" in value
        or "\n" in value
    ):
        return default
    return value


def _require_existing_page(page: int, total_pages: int) -> None:
    if page > total_pages:
        raise WebInputError(f"页码超出范围; 当前结果共 {total_pages} 页。")


def _content_disposition(filename: str, ascii_filename: str) -> str:
    if (
        any(ord(character) < 32 or ord(character) == 127 for character in filename)
        or '"' in filename
        or _ASCII_DOWNLOAD_NAME.fullmatch(ascii_filename) is None
    ):
        raise WebInputError("导出文件名无效。")
    return f"attachment; filename=\"{ascii_filename}\"; filename*=UTF-8''{quote(filename, safe='')}"


def _form_str(form: object, key: str, default: str = "") -> str:
    from fastapi.datastructures import FormData

    if not isinstance(form, FormData):
        return default
    val = form.get(key)
    if not isinstance(val, str):
        return default
    return val.strip() or default


def _form_int(form: object, key: str, default: int) -> int:
    from fastapi.datastructures import FormData

    if not isinstance(form, FormData):
        return default
    val = form.get(key)
    if isinstance(val, str) and val.isdigit():
        return int(val)
    return default


def _parse_ids(raw: str) -> list[int]:
    return [int(s) for s in raw.split(",") if s.strip().isdigit()]
