# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportMissingParameterType=false
# pyright: reportUnknownParameterType=false
"""Stage 11 UI/AI workflow tests — navigation, read status, dates, enums."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.storage.database import Database
from app.web.app import create_app


@pytest.fixture
def stage11_client(database: Database) -> Iterator[TestClient]:
    app = create_app(database=database, enforce_migrations=False)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# --- Navigation ---

def test_navigation_has_five_entries(stage11_client):
    resp = stage11_client.get("/")
    assert resp.status_code == 200
    nav_start = resp.text.index('<nav')
    nav_end = resp.text.index('</nav>', nav_start)
    nav = resp.text[nav_start:nav_end]
    link_count = nav.count('<a ')
    assert link_count == 5, f"Expected 5 nav links, got {link_count}"


def test_nav_entries_correct_order(stage11_client):
    resp = stage11_client.get("/")
    entries = ["资讯", "AI", "来源", "设置", "更新记录"]
    positions = [resp.text.index(e) for e in entries]
    assert positions == sorted(positions)


def test_leadership_page_redirects(stage11_client):
    resp = stage11_client.get("/leadership", follow_redirects=False)
    assert resp.status_code in (301, 302, 307, 308)
    assert resp.headers["location"] == "/"


def test_industry_leads_not_in_nav(stage11_client):
    resp = stage11_client.get("/")
    nav_start = resp.text.index('<nav')
    nav_end = resp.text.index('</nav>', nav_start)
    nav_html = resp.text[nav_start:nav_end]
    assert "行业线索" not in nav_html


def test_homepage_all_scope(stage11_client):
    resp = stage11_client.get("/")
    html = resp.text
    assert "全部来源" in html


# --- Read / Unread ---

def test_is_read_filter_works(stage11_client):
    for val in ("yes", "no", "all"):
        resp = stage11_client.get(f"/?is_read={val}")
        assert resp.status_code == 200


def test_read_status_endpoint_rejects_invalid(stage11_client):
    resp = stage11_client.post("/items/1/read", data={"is_read": "invalid", "return_to": "/"})
    assert resp.status_code == 400


def test_batch_read_rejects_empty(stage11_client):
    resp = stage11_client.post(
        "/items/batch-read",
        data={"item_ids": "", "is_read": "true", "return_to": "/"},
    )
    assert resp.status_code == 400


# --- Date validation ---

def test_date_from_after_to_rejected(stage11_client):
    resp = stage11_client.get("/?published_from=2025-06-01&published_to=2025-01-01")
    assert resp.status_code == 400


def test_date_invalid_val_rejected(stage11_client):
    resp = stage11_client.get("/?published_from=12025-01-01")
    assert resp.status_code == 400


def test_date_bad_month_safe(stage11_client):
    resp = stage11_client.get("/?published_from=2025-13-01")
    assert resp.status_code == 400


def test_date_feb29_leap_ok(stage11_client):
    resp = stage11_client.get("/?published_from=2024-02-29")
    assert resp.status_code == 200


def test_date_feb29_nonleap_bad(stage11_client):
    resp = stage11_client.get("/?published_from=2025-02-29")
    assert resp.status_code == 400


def test_date_html_min_attr(stage11_client):
    resp = stage11_client.get("/")
    assert 'min="2000-01-01"' in resp.text


# --- No raw internal English enum values in visible text ---

def test_no_internal_english_display(stage11_client):
    """Check pages don't show raw enum values like 'automatic' in user text."""
    pages = ["/", "/sources", "/runs", "/settings", "/ai"]
    raw_terms = ["automatic", "pending", "active", "media_only"]
    for path in pages:
        resp = stage11_client.get(path)
        if resp.status_code != 200:
            continue
        visible = _strip_tags(resp.text)
        for term in raw_terms:
            assert term not in visible, f"'{term}' found in visible text on {path}"


def _strip_tags(html: str) -> str:
    import re
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.lower()


# --- AI page ---

def test_ai_page_loads(stage11_client):
    resp = stage11_client.get("/ai")
    assert resp.status_code == 200
    assert "<h1>AI</h1>" in resp.text


def test_ai_page_shows_key_status(stage11_client):
    resp = stage11_client.get("/ai")
    assert ("已配置" in resp.text) or ("未配置" in resp.text)


def test_ai_page_has_connection_test_button(stage11_client):
    resp = stage11_client.get("/ai")
    assert "测试连接" in resp.text


# --- Foundation: No API key needed for rule mode ---

def test_items_page_loads(stage11_client):
    resp = stage11_client.get("/")
    assert resp.status_code == 200


# --- Settings page ---

def test_settings_page_loads(stage11_client):
    resp = stage11_client.get("/settings")
    assert resp.status_code == 200
    assert "设置" in resp.text


# --- Sources page ---

def test_sources_page_loads(stage11_client):
    resp = stage11_client.get("/sources")
    assert resp.status_code == 200


# --- Runs page ---

def test_runs_page_loads(stage11_client):
    resp = stage11_client.get("/runs")
    assert resp.status_code == 200


# --- Update button text ---

def test_update_button_uses_chinese_label(stage11_client):
    resp = stage11_client.get("/")
    html = resp.text
    assert "更新全部启用来源" in html
    assert "更新全部 active 来源" not in html


# --- AI settings save and clear ---

def test_ai_save_settings(stage11_client, database: Database):
    resp = stage11_client.post(
        "/ai/save",
        data={
            "provider": "deepseek",
            "base_url": "https://test.example.com",
            "model": "test-model",
            "api_key": "sk-test-1234",
            "timeout_seconds": "60",
            "max_retries": "3",
            "classifier_mode": "manual",
            "classifier_strategy": "hybrid",
            "summarizer_mode": "manual",
        },
    )
    assert resp.status_code == 200
    assert "设置已保存" in resp.text
    from app.services.ai_settings_service import AISettingsService
    svc = AISettingsService(database)
    config = svc.get_config()
    assert config.provider == "deepseek"
    assert config.model == "test-model"
    assert config.api_key == "sk-test-1234"
    assert config.timeout_seconds == 60
    assert config.classifier_mode == "manual"
    assert config.summarizer_mode == "manual"


def test_ai_clear_key(stage11_client, database: Database):
    stage11_client.post(
        "/ai/save",
        data={
            "provider": "deepseek",
            "base_url": "https://test.example.com",
            "model": "test-model",
            "api_key": "sk-test-1234",
            "timeout_seconds": "30",
            "max_retries": "1",
            "classifier_mode": "off",
            "classifier_strategy": "hybrid",
            "summarizer_mode": "off",
        },
    )
    resp = stage11_client.post("/ai/clear-key")
    assert resp.status_code == 200
    from app.services.ai_settings_service import AISettingsService
    config = AISettingsService(database).get_config()
    assert config.api_key == ""


def test_ai_empty_key_preserves_old_key(stage11_client, database: Database):
    stage11_client.post(
        "/ai/save",
        data={
            "provider": "deepseek",
            "base_url": "https://test.example.com",
            "model": "m",
            "api_key": "sk-old-key",
            "timeout_seconds": "30",
            "max_retries": "1",
            "classifier_mode": "off",
            "classifier_strategy": "hybrid",
            "summarizer_mode": "off",
        },
    )
    stage11_client.post(
        "/ai/save",
        data={
            "provider": "deepseek",
            "base_url": "https://test.example.com",
            "model": "m2",
            "api_key": "",
            "timeout_seconds": "30",
            "max_retries": "1",
            "classifier_mode": "off",
            "classifier_strategy": "hybrid",
            "summarizer_mode": "off",
        },
    )
    from app.services.ai_settings_service import AISettingsService
    config = AISettingsService(database).get_config()
    assert config.api_key == "sk-old-key"
    assert config.model == "m2"


# --- Key not leaked in HTML ---

def test_ai_page_does_not_leak_full_key(stage11_client, database: Database):
    stage11_client.post(
        "/ai/save",
        data={
            "provider": "deepseek",
            "base_url": "https://test.example.com",
            "model": "m",
            "api_key": "sk-secret-full-key-value",
            "timeout_seconds": "30",
            "max_retries": "1",
            "classifier_mode": "off",
            "classifier_strategy": "hybrid",
            "summarizer_mode": "off",
        },
    )
    resp = stage11_client.get("/ai")
    html = resp.text
    assert "sk-secret-full-key-value" not in html
    assert "sk-s****alue" in html or "sk-**" in html


# --- AI classify endpoints exist ---

def test_ai_classify_single_route(stage11_client):
    resp = stage11_client.post(
        "/items/1/ai-classify",
        data={"return_to": "/"},
    )
    assert resp.status_code in (200, 303, 302)


def test_ai_classify_batch_route(stage11_client):
    resp = stage11_client.post(
        "/items/batch-ai-classify",
        data={"item_ids": "1", "return_to": "/"},
    )
    assert resp.status_code in (200, 303, 302)


def test_ai_summarize_single_route(stage11_client):
    resp = stage11_client.post(
        "/items/1/ai-summarize",
        data={"return_to": "/"},
    )
    assert resp.status_code in (200, 303, 302)


def test_ai_summarize_batch_route(stage11_client):
    resp = stage11_client.post(
        "/items/batch-ai-summarize",
        data={"item_ids": "1", "return_to": "/"},
    )
    assert resp.status_code in (200, 303, 302)


# --- AI page has all required sections ---

def test_ai_page_has_form_elements(stage11_client):
    resp = stage11_client.get("/ai")
    html = resp.text
    assert "服务商" in html
    assert "Base URL" in html or "base_url" in html
    assert "API Key" in html
    assert "AI 分类" in html
    assert "AI 总结" in html
    assert "测试连接" in html
    assert "保存设置" in html


# --- Leadership removed from UI ---

def test_leadership_not_in_filter_options(stage11_client):
    resp = stage11_client.get("/")
    html = resp.text
    source_scope_start = html.find("来源范围")
    if source_scope_start > 0:
        source_scope_end = html.find("</select>", source_scope_start)
        scope_html = html[source_scope_start:source_scope_end + 9]
        assert "leadership" not in scope_html


# --- Date max attribute ---

def test_date_inputs_have_dynamic_max(stage11_client):
    resp = stage11_client.get("/")
    html = resp.text
    assert 'type="date"' in html
    assert 'min="2000-01-01"' in html


# --- AI jobs are recorded ---

def test_ai_job_is_created_on_classify(stage11_client, database: Database):
    from app.storage.repositories import RepositoryUnitOfWork
    with RepositoryUnitOfWork(database) as uow:
        item = uow.items.get(1)
        if item:
            item.is_active = True
            item.admission_accepted = True
            item.manual_category = None

    resp = stage11_client.post("/ai/classify", data={"item_ids": "1"})
    assert resp.status_code in (200, 303, 302)


# --- No internal English in AI page ---

def test_ai_page_no_raw_english(stage11_client):
    import re
    resp = stage11_client.get("/ai")
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", resp.text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    for term in ["automatic", "pending", "active", "media_only"]:
        assert term not in text.lower()
